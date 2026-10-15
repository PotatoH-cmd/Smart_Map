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
        // 确定性检索：消除 Agent Search 的 LLM 变体/评估随机性，同 query 结果可复现
        KB_DETERMINISTIC: "1",
        // DASHSCOPE_API_KEY 由 main.py load_dotenv(backend/.env) 注入，勿在此重复明文
      },
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      // 报告生成等重型任务（大影像裁剪 + 知识库索引）内存峰值可达 3~5GB，
      // 默认 2GB 上限会触发 OOM 重启中断任务；机器内存充足（500GB+），放宽到 8G。
      max_memory_restart: "8G",
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

