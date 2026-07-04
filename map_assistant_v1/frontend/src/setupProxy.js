const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function (app) {
  // 将 /api 请求代理到后端，不受 Accept: text/html 头的限制
  // CRA 默认 proxy 在遇到 Accept: text/html 时会返回 index.html（SPA 兜底），
  // 导致 /api/preview/report 等需要在浏览器中直接打开的页面无法正常代理
  const apiProxy = createProxyMiddleware({
    target: 'http://localhost:8006',
    changeOrigin: true,
  });

  app.use('/api', apiProxy);
  app.use('/chat', apiProxy);
  app.use('/sessions', apiProxy);
  app.use('/suggestions', apiProxy);
  app.use('/tiles', apiProxy);
  app.use('/mvt', apiProxy);
};
