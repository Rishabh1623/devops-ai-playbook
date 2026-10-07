const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function (app) {
  app.use(
    '/api',
    createProxyMiddleware({
      target: 'http://localhost:3001', // API gateway, same as nginx in Docker
      changeOrigin: true,
    })
  );
};