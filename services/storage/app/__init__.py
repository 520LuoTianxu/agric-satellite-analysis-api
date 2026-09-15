"""storage 服务：只消费 Celery ``storage`` 队列，把文件上传到对象存储（默认阿里云 OSS）。

本包不提供 HTTP API，也不做栅格计算。ingest / API 通过
``app.tasks.storage.*`` 把上传交给这里，镜像刻意不装 GDAL。
"""
