"use client";

import cytoscape, {
  type Core,
  type EdgeSingular,
  type ElementDefinition,
  type NodeSingular,
} from "cytoscape";
import { useEffect, useRef } from "react";

import type { RiskBand } from "@/lib/api";
import { isGroup, type ViewEdge, type ViewNode } from "@/lib/graphView";

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

/** Risk bands come from the API, so the canvas and the ranking cannot disagree. */
const BAND_COLOR: Record<RiskBand, string> = {
  low: "#3fb950",
  moderate: "#d29922",
  high: "#f0883e",
  critical: "#f85149",
};

/** A node the risk ranking never reached — grey, not green. Unscored is not safe. */
const UNSCORED = "#484f58";
const GROUP_COLOR = "#8957e5";
const IMPACT_COLOR = "#f0883e";

export type ColorMode = "type" | "risk";

export interface ImpactOverlay {
  rootKey: string;
  /** View-node key → hops from the root. */
  hopsByKey: Map<string, number>;
  /** `"<source> <target>"` for each edge on an impact route. */
  routeEdges: Set<string>;
}

export interface GraphCanvasProps {
  nodes: ViewNode[];
  edges: ViewEdge[];
  selected: string | null;
  onSelect: (key: string | null) => void;
  colorMode?: ColorMode;
  riskByKey?: Map<string, { score: number; band: RiskBand }>;
  /** Set to light up the blast radius; null shows the plain graph. */
  impact?: ImpactOverlay | null;
}

function toElements(nodes: ViewNode[], edges: ViewEdge[]): ElementDefinition[] {
  const present = new Set(nodes.map((n) => n.key));
  return [
    ...nodes.map((n) => {
      const group = isGroup(n);
      return {
        data: {
          id: n.key,
          // Long qualnames and deep paths make the canvas unreadable; the full
          // value stays in `title` for the details panel.
          label: group
            ? `${n.display} (${n.size})`
            : n.display.length > 34
              ? `…${n.display.slice(-33)}`
              : n.display,
          title: n.display,
          type: n.type,
          group,
          size: group ? n.size : 1,
        },
      };
    }),
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
          merged: e.merged,
        },
      })),
  ];
}

export default function GraphCanvas({
  nodes,
  edges,
  selected,
  onSelect,
  colorMode = "type",
  riskByKey,
  impact = null,
}: GraphCanvasProps) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);

  // Colour is read from refs inside the style mappers so switching mode or
  // loading risk scores repaints without rebuilding elements — a rebuild would
  // re-run the layout and throw away the user's pan, zoom and mental map.
  const modeRef = useRef(colorMode);
  const riskRef = useRef(riskByKey);
  modeRef.current = colorMode;
  riskRef.current = riskByKey;

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
            "background-color": (n) => {
              if (n.data("group")) return GROUP_COLOR;
              if (modeRef.current === "risk") {
                const band = riskRef.current?.get(n.id())?.band;
                return band ? BAND_COLOR[band] : UNSCORED;
              }
              return NODE_COLOR[n.data("type")] ?? "#8b949e";
            },
            label: "data(label)",
            color: "#e6edf3",
            "font-size": 8,
            "text-valign": "center",
            "text-halign": "right",
            "text-margin-x": 4,
            // Collapsed modules carry their member count as area, so a big
            // package reads as big without a legend.
            width: (n: NodeSingular) =>
              n.data("group") ? Math.min(46, 14 + 3 * Math.sqrt(n.data("size"))) : 12,
            height: (n: NodeSingular) =>
              n.data("group") ? Math.min(46, 14 + 3 * Math.sqrt(n.data("size"))) : 12,
            "border-width": 0,
          },
        },
        {
          selector: "edge",
          style: {
            width: (e: EdgeSingular) => (e.data("merged") > 1 ? 2 : 1),
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
        // --- blast radius overlay ---
        {
          selector: ".faded",
          style: { opacity: 0.1, "text-opacity": 0 },
        },
        {
          selector: "node.impacted",
          style: {
            "background-color": IMPACT_COLOR,
            // Nearer nodes are more likely to actually break, so they read
            // stronger. Beyond four hops everything looks the same anyway.
            opacity: (n: NodeSingular) =>
              Math.max(0.45, 1 - 0.18 * ((n.data("hops") as number) - 1)),
            "border-width": 1,
            "border-color": IMPACT_COLOR,
            "z-index": 5,
          },
        },
        {
          selector: "node.impact-root",
          style: {
            "background-color": "#f0f6fc",
            "border-width": 4,
            "border-color": IMPACT_COLOR,
            width: 22,
            height: 22,
            "font-size": 12,
            "z-index": 20,
            opacity: 1,
          },
        },
        {
          selector: "edge.route",
          style: {
            "line-color": IMPACT_COLOR,
            "line-opacity": 0.85,
            width: 2,
            "z-index": 6,
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

  // Repaint on colour-mode or risk-data change. No layout, no rebuild.
  useEffect(() => {
    cy.current?.style().update();
  }, [colorMode, riskByKey]);

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

  // The blast radius. Applied as classes in one batch rather than by rebuilding
  // elements, so lighting up dependents is instant and the layout holds still.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;

    instance.batch(() => {
      instance.elements().removeClass("faded impacted impact-root route");
      if (!impact) return;

      instance.nodes().forEach((node) => {
        const id = node.id();
        if (id === impact.rootKey) {
          node.addClass("impact-root");
          return;
        }
        const hops = impact.hopsByKey.get(id);
        if (hops === undefined) {
          node.addClass("faded");
          return;
        }
        node.data("hops", hops);
        node.addClass("impacted");
      });

      instance.edges().forEach((edge) => {
        const pair = `${edge.data("source")} ${edge.data("target")}`;
        edge.addClass(impact.routeEdges.has(pair) ? "route" : "faded");
      });
    });
  }, [impact]);

  return <div ref={container} className="graph-canvas" />;
}
