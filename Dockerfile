FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# 设置时区与系统环境变量
ENV TZ=Asia/Shanghai
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN if [ -f /usr/share/zoneinfo/$TZ ]; then ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone; fi


WORKDIR /app

# 安装 Python 依赖
# 基础镜像已内置 Chromium 内核，无需重复下载浏览器
# 配置阿里云 + 腾讯云双商业高速镜像，设置 120s 超时与 5 次重试，杜绝外网请求与卡顿
COPY requirements.txt .
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.extra-index-url https://mirrors.cloud.tencent.com/pypi/simple/ \
    && pip config set global.trusted-host "mirrors.aliyun.com mirrors.cloud.tencent.com" \
    && pip config set global.timeout 120 \
    && pip install --no-cache-dir --retries 5 -r requirements.txt

# 复制工程源码
COPY . .

# 创建持久化数据目录
RUN mkdir -p /app/data

# 暴露飞牛专用端口 8836
EXPOSE 8836

# 启动 FastAPI 服务 (启用热重载，代码更新秒级生效无需反复 rebuild)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8836", "--reload"]

