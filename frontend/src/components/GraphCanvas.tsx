"use client";

import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";

import type { GraphEdge, GraphNode } from "@/lib/api";

/** Colour per node label — the fastest way to read structure at a glance. */
const NODE_COLOR: Record<string, string> = {
  Repo: "#d29922",
  File: "#58a6ff",
  Class: "#bc8cff",
  Function: "#3fb950",
  Module: "#8b949e",
};

const EDGE_COLOR: Record<string, string> = {
  CONTAINS: "#30363d",
  IMPORTS: "#58a6ff",
  CALLS: "#3fb950",
  INHERITS: "#bc8cff",
};

export interface GraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected: string | null;
  onSelect: (key: string | null) => void;
}

function toElements(nodes: GraphNode[], edges: GraphEdge[]): ElementDefinition[] {
  const present = new Set(nodes.map((n) => n.key));
  return [
    ...nodes.map((n) => ({
      data: {
        id: n.key,
        // Long qualnames and deep paths make the canvas unreadable; the full
        // value stays in `title` for the details panel.
        label: n.display.length > 34 ? `…${n.display.slice(-33)}` : n.display,
        title: n.display,
        type: n.type,
      },
    })),
    // An edge whose endpoint was cut by the node cap would make Cytoscape
    // throw, so drop it rather than render a partial graph badly.
    ...edges
      .filter((e) => present.has(e.source) && present.has(e.target))
      .map((e, i) => ({
        data: {
          id: `e${i}`,
          source: e.source,
          target: e.target,
          type: e.type,
          // Confidence is opacity: a guessed call should look less certain
          // than a resolved import, not identical to it.
          weight: e.confidence ?? 1,
        },
      })),
  ];
}

export default function GraphCanvas({
  nodes,
  edges,
  selected,
  onSelect,
}: GraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);

  useEffect(() => {
    if (!container.current) return;

    const instance = cytoscape({
      container: container.current,
      elements: toElements(nodes, edges),
      // Rendering every label while panning a few hundred nodes is what makes
      // these canvases feel frozen; hiding them during interaction keeps the
      // frame rate up without changing the static view.
      textureOnViewport: true,
      hideEdgesOnViewport: true,
      pixelRatio: 1,
      style: [
        {
          selector: "node",
          style: {
            "background-color": (n) => NODE_COLOR[n.data("type")] ?? "#8b949e",
            label: "data(label)",
            color: "#e6edf3",
            "font-size": 8,
            "text-valign": "center",
            "text-halign": "right",
            "text-margin-x": 4,
            width: 12,
            height: 12,
            "border-width": 0,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": (e) => EDGE_COLOR[e.data("type")] ?? "#30363d",
            "line-opacity": (e) => 0.25 + 0.45 * (e.data("weight") as number),
            "curve-style": "haystack",
            "haystack-radius": 0,
          },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 3,
            "border-color": "#f0f6fc",
            width: 18,
            height: 18,
            "font-size": 11,
            "z-index": 10,
          },
        },
      ],
      layout: {
        name: "cose",
        animate: false,
        // Bounded rather than run to convergence: cose on several hundred
        // nodes will happily spin for tens of seconds otherwise, which is the
        // "frozen browser" this day is measured against.
        numIter: 400,
        nodeRepulsion: () => 12000,
        idealEdgeLength: () => 60,
        nodeOverlap: 8,
      },
      minZoom: 0.05,
      maxZoom: 4,
    });

    instance.on("tap", "node", (event) => onSelect(event.target.id()));
    // A tap on empty canvas clears the selection.
    instance.on("tap", (event) => {
      if (event.target === instance) onSelect(null);
    });

    cy.current = instance;
    return () => {
      instance.destroy();
      cy.current = null;
    };
  }, [nodes, edges, onSelect]);

  // Selection is driven from React so the details panel and the canvas cannot
  // disagree about what is selected.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    instance.elements(":selected").unselect();
    if (!selected) return;
    const node = instance.getElementById(selected);
    if (node.length) {
      node.select();
      instance.animate({ center: { eles: node } }, { duration: 200 });
    }
  }, [selected]);

  return <div ref={container} className="graph-canvas" />;
}
