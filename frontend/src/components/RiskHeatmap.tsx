"use client";

import { useState } from "react";

import type { RiskItem } from "@/lib/api";

/**
 * Risk per file, as a grid of cells.
 *
 * **Sequential, not categorical.** Risk is ordered — low through critical — so
 * the encoding is one hue stepping light to dark, not four separate hues. The
 * app's band colours (green/amber/orange/red) fail a colour-vision check as a
 * categorical set: `moderate` and `high` sit ΔE 1.5 apart for deuteranopes and
 * 7.0 for normal vision, which is precisely the pair a reader must separate.
 * This ramp is validated instead for what a sequential ramp is judged on —
 * monotone lightness, an adjacent lightness gap of at least 0.06, a single hue
 * (30° spread), and a light end that clears 2:1 against the panel.
 *
 * Colour is never the only channel: every cell names its band in the tooltip,
 * and the top-ten table beside this grid is the table view.
 */
export const RISK_RAMP: Array<[string, string]> = [
  ["low", "#6b4642"],
  ["moderate", "#a8503d"],
  ["high", "#dc6b3f"],
  ["critical", "#f8796d"],
];

const COLOR_BY_BAND: Record<string, string> = Object.fromEntries(RISK_RAMP);

/** A file the ranking never reached. Grey, never the ramp's low step — absent
 *  from the top 200 is not the same as measured and found safe. */
const UNSCORED = "#30363d";

export interface RiskHeatmapProps {
  items: RiskItem[];
  /** Called when a cell is chosen, so the page can show the full derivation. */
  onSelect?: (item: RiskItem) => void;
  selected?: string | null;
}

export default function RiskHeatmap({ items, onSelect, selected }: RiskHeatmapProps) {
  const [hovered, setHovered] = useState<RiskItem | null>(null);

  if (!items.length) {
    return <p className="empty">No files scored yet.</p>;
  }

  return (
    <div className="heatmap-wrap">
      <div className="heatmap" role="list">
        {items.map((item) => (
          <button
            key={item.key}
            role="listitem"
            className={`heat-cell${selected === item.key ? " selected" : ""}`}
            style={{ background: COLOR_BY_BAND[item.band] ?? UNSCORED }}
            onMouseEnter={() => setHovered(item)}
            onFocus={() => setHovered(item)}
            onMouseLeave={() => setHovered(null)}
            onBlur={() => setHovered(null)}
            onClick={() => onSelect?.(item)}
            // The band is in the label, so the cell is never colour-alone.
            aria-label={`${item.display}, risk ${item.score.toFixed(0)}, ${item.band}`}
            title={`${item.display} — ${item.score.toFixed(1)} (${item.band})`}
          />
        ))}
      </div>

      {/* Reserved height: a tooltip that appears and disappears must not reflow
          the grid above it every time the pointer moves. */}
      <div className="heat-readout">
        {hovered ? (
          <>
            <span className="mono">{hovered.display}</span>
            <span className="muted">
              {" "}
              — {hovered.score.toFixed(1)} ({hovered.band})
              {hovered.reasons.length > 0 && ` · ${hovered.reasons[0]}`}
            </span>
          </>
        ) : (
          <span className="muted">
            {items.length} files, worst first. Hover a cell; click for the full score.
          </span>
        )}
      </div>

      <div className="legend" aria-hidden="true">
        {RISK_RAMP.map(([band, color]) => (
          <span key={band} className="legend-item">
            <i style={{ background: color, borderRadius: 2 }} /> {band}
          </span>
        ))}
      </div>
    </div>
  );
}
