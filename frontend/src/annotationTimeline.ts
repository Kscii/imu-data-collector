export type TimelineInterval = {
  key: string;
  start_ns: number;
  end_ns: number;
};

/**
 * 标注区间统一采用左闭右开语义 [start, end)。
 * 这与后端验证、训练导出的 sample_start/sample_stop 定义保持一致。
 */
export function intervalsAtTime<T extends TimelineInterval>(
  intervals: T[],
  timeNs: number,
): T[] {
  return intervals.filter((item) => timeNs >= item.start_ns && timeNs < item.end_ns);
}

/**
 * 自动跟随时让当前区间尽量成为第二条可见卡片。
 * 第一条没有前项，因此只能以自身作为顶部锚点。
 */
export function intervalFollowAnchorIndex<T extends TimelineInterval>(
  intervals: T[],
  activeKey: string,
): number {
  const activeIndex = intervals.findIndex((item) => item.key === activeKey);
  return activeIndex < 0 ? -1 : Math.max(0, activeIndex - 1);
}
