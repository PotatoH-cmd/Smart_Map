/* eslint-disable no-restricted-globals */
self.onmessage = (e) => {
  try {
    const { text } = e.data || {};
    const obj = JSON.parse(text);
    // 这里保留纯解析，简化可在后续版本加入（如 Douglas-Peucker）
    self.postMessage(obj);
  } catch (err) {
    self.postMessage({ type: 'FeatureCollection', features: [], _error: String(err) });
  }
};
