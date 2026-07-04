module.exports = {
  webpack: {
    configure: (webpackConfig) => {
      webpackConfig.output = webpackConfig.output || {};
      webpackConfig.output.environment = {
        ...(webpackConfig.output.environment || {}),
        // 禁用 ESM 模块输出，避免 Cesium import.meta 在非 module 脚本中报错
        module: false,
      };
      return webpackConfig;
    },
  },
};
