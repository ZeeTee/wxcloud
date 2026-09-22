# 日报归档服务 —— 微信云托管镜像
#
# 相对官方模板的改动(每一处都有理由,不要无意中改回去):
#
#   1. 基础镜像 alpine:3.13 → python:3.12-alpine。
#      Django 5.2 要求 Python ≥ 3.10,而 alpine:3.13 自带的是早已 EOL 的 Python 3.9。
#      官方 python 镜像自带正确版本的 Python 与 pip,比 apk 装更可靠。
#
#   2. 先 COPY requirements.txt 再 COPY 代码,让"只改代码"时能复用依赖层,
#      每次构建省下装依赖的时间。
#
#   3. 装 tzdata:设定 TIME_ZONE=Asia/Shanghai 后,Django 需要系统时区数据库,
#      而 alpine 默认不带。
#
#   4. 启动前跑 migrate:容器是长驻的,启动即自愈,不必依赖控制台手工建表。
#      (maxNum 已配成 1,不存在多副本并发迁移的问题。)
#
#   5. 用 gunicorn 而不是 `manage.py runserver` —— 后者是开发服务器。
#
# 如需临时退回开发服务器调试,把最后一行 CMD 换成:
#   CMD ["python3", "manage.py", "runserver", "0.0.0.0:80"]

FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区数据库(见上面第 3 条)
RUN apk add --no-cache tzdata

# 依赖层:只有 requirements.txt 变了才需要重装
COPY requirements.txt /app/requirements.txt
RUN pip config set global.index-url http://mirrors.cloud.tencent.com/pypi/simple \
 && pip config set global.trusted-host mirrors.cloud.tencent.com \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# 代码层
COPY . /app

# 必须与「服务设置」里的端口一致,否则部署失败
EXPOSE 80

# migrate 加 --fake-initial:万一表是人工先建好的,迁移不会因"表已存在"而中断启动。
CMD ["sh", "-c", "python3 manage.py migrate --noinput --fake-initial && exec gunicorn wxcloudrun.wsgi:application --bind 0.0.0.0:80 --workers 1 --threads 4 --timeout 30 --access-logfile - --error-logfile -"]
