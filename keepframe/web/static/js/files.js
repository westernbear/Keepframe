function readFileAs(file, method) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader[method](file);
  });
}

function readFileAsDataUrl(file) {
  if (!file) return Promise.resolve(null);
  return readFileAs(file, "readAsDataURL");
}

async function readFileAsBase64(file) {
  if (!file) return null;
  const dataUrl = await readFileAsDataUrl(file);
  const comma = String(dataUrl).indexOf(",");
  const hasPayload = comma >= 0;
  return hasPayload ? String(dataUrl).slice(comma + 1) : String(dataUrl);
}

export { readFileAsDataUrl, readFileAsBase64 };
