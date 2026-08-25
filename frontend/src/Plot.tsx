import { useEffect, useRef } from "react";
import uPlot from "uplot";

const colors = ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4"];
const labels = ["ax", "ay", "az", "gx", "gy", "gz"];

type Props = {
  time: number[];
  values: number[][];
  cursorTime?: number;
  height?: number;
};

export default function Plot({ time, values, cursorTime, height = 290 }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const width = Math.max(480, host.current.clientWidth);
    plot.current = new uPlot(
      {
        width,
        height,
        cursor: { drag: { x: true, y: false } },
        scales: { x: { time: false } },
        axes: [
          { stroke: "#94a3b8", grid: { stroke: "#253046" } },
          { stroke: "#94a3b8", grid: { stroke: "#253046" } }
        ],
        series: [
          { label: "time" },
          ...labels.map((label, index) => ({
            label,
            stroke: colors[index],
            width: 1.4,
            points: { show: false }
          }))
        ]
      },
      [[], [], [], [], [], [], []],
      host.current
    );
    const observer = new ResizeObserver(() => {
      if (host.current && plot.current) {
        plot.current.setSize({ width: host.current.clientWidth, height });
      }
    });
    observer.observe(host.current);
    return () => {
      observer.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
  }, [height]);

  useEffect(() => {
    if (!plot.current) return;
    const columns = Array.from({ length: 6 }, (_, axis) =>
      values.map((row) => row[axis] ?? 0)
    );
    plot.current.setData([time, ...columns]);
  }, [time, values]);

  useEffect(() => {
    if (plot.current && cursorTime !== undefined && time.length) {
      plot.current.setCursor({ left: plot.current.valToPos(cursorTime, "x"), top: 0 });
    }
  }, [cursorTime, time]);

  return <div className="plot" ref={host} />;
}
