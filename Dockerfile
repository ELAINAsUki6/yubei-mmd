FROM python:3.12-slim

WORKDIR /app

# 只拷运行需要的东西，截图和 README 不用进镜像
COPY server.py index.html ./

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# 绑定 0.0.0.0，容器外面才连得进来；对外的 HTTPS 交给 nginx / Cloudflare
CMD ["python", "-u", "server.py", "8000", "0.0.0.0"]
