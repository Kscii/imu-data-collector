import { useEffect, useRef } from "react";
import uPlot from "uplot";

const colors = ["#ef4444", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#06b6d4"];
const labels = ["ax", "ay", "az", "gx", "gy", "gz"];

type Props = {
  time: number[];
  values: number[][];
  cursorTime?: number;
  height?: number;
  onSelectTime?: (time: number) => void;
};

export default function Plot({ time, values, cursorTime, height = 290, onSelectTime }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const onSelectTimeRef = useRef(onSelectTime);

  useEffect(() => {
    onSelectTimeRef.current = onSelectTime;
  }, [onSelectTime]);

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
    const select = (event: MouseEvent) => {
      if (!plot.current || !host.current || !onSelectTimeRef.current) return;
      const bounds = host.current.getBoundingClientRect();
      const value = plot.current.posToVal(event.clientX - bounds.left, "x");
      onSelectTimeRef.current(value);
    };
    host.current.addEventListener("click", select);
    return () => {
      host.current?.removeEventListener("click", select);
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
