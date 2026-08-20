module.exports = {
  apps: [
    {
      name: "map-assistant-backend",
      script: "/home/server/miniconda3/envs/mapagent6/bin/python3",
      args: "main.py",
      cwd: "/home/server/python/map_assistant_v1/backend",
      interpreter: "none",
      env: {
        PORT: "8006",
        USE_INTENT_AGENT: "true",
        CUDA_VISIBLE_DEVICES: "1",
        SATELLITE_TIF_PATH: "/mnt/arcgisorgdata/2026001_河南省2026年1_2月亚米遥感影像/河南2026年高分第一季度影像.tif",
        HF_ENDPOINT: "https://hf-mirror.com",
        // 知识库后端切换为 LlamaIndex
        KNOWLEDGE_BACKEND: "llamaindex",
        LLAMAINDEX_PERSIST_DIR: "./llama_index_storage",
        DASHSCOPE_API_KEY: "sk-e4990da94bfb4037be1f755fa586d048",
      },
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      out_file: "/home/server/python/map_assistant_v1/backend/pm2_out.log",
      error_file: "/home/server/python/map_assistant_v1/backend/pm2_err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
    {
      name: "map-assistant-frontend",
      script: "npm",
      args: "start",
      cwd: "/home/server/python/map_assistant_v1/frontend",
      interpreter: "none",
      // 开发服务器有自己的文件监听，pm2 不需要 watch
      watch: false,
      // 开发服务器一般不会崩溃，限制重启次数避免无限重启
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 5,
      env: {
        // 禁用浏览器自动打开
        BROWSER: "none",
        PORT: "3004",
        // 开发服务器将 API 请求代理到后端（package.json 里的 proxy 字段生效）
      },
      out_file: "/home/server/python/map_assistant_v1/frontend/pm2_out.log",
      error_file: "/home/server/python/map_assistant_v1/frontend/pm2_err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
    },
  ],
};

