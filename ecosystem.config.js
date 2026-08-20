/**
 * map_assistant_v1 PM2 托管配置
 * 启动：npx pm2 start ecosystem.config.js
 * 重启：npx pm2 restart map-assistant-backend / map-assistant-frontend
 * 查看：npx pm2 list / npx pm2 logs
 */
module.exports = {
  apps: [
    {
      name: 'map-assistant-backend',
      script: '/home/server/miniconda3/envs/mapagent6/bin/python3',
      args: 'main.py',
      cwd: '/home/server/python/map_assistant_v1/backend',
      env: {
        // 灰度开关：on=新 RunEngine 路径，off=旧 execute_stream 路径（回退用）
        RUN_ENGINE: 'on',
      },
      max_memory_restart: '1G',
      time: true,
    },
    {
      name: 'map-assistant-frontend',
      script: 'npm',
      args: 'start',
      cwd: '/home/server/python/map_assistant_v1/frontend',
      env: {
        // CRA dev server 端口（3000 被其他容器占用，沿用历史端口 3004）
        PORT: '3004',
        // 防止 npm start 自动打开浏览器
        BROWSER: 'none',
      },
      max_memory_restart: '1G',
      time: true,
    },
  ],
};
