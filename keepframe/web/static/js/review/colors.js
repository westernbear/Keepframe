// Object IDs, not list order or frame number, determine the shared layer/list/track color.
export function objectColor(id) {
  let hash = 0;
  for (const c of id) hash = ((hash * 31) + c.charCodeAt(0)) >>> 0;
  return `hsl(${(hash * 137.508) % 360} 78% 62%)`;
}
