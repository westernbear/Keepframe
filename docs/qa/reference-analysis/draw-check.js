(() => {
  const layer = document.querySelector('#orig-draw');
  const svg = document.querySelector('#orig-overlay');
  const rect = svg.getBoundingClientRect();
  const [,,w,h] = svg.getAttribute('viewBox').split(' ').map(Number);
  const emit = (type,x,y) => layer.dispatchEvent(new PointerEvent(type, { bubbles:true, pointerId:1, button:0, buttons:type === 'pointerup' ? 0 : 1, clientX:rect.left+x/w*rect.width, clientY:rect.top+y/h*rect.height }));
  emit('pointerdown', 349, 160); emit('pointermove', 415, 205); emit('pointerup', 415, 205);
  return {coords:document.querySelector('#bbox-coords').value, frame:document.querySelector('#bbox-frame').value, mode:document.querySelector('#region-mode').getAttribute('aria-pressed')};
})()
