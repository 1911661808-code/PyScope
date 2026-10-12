# 从仓库根目录构建：docker build -f sandbox/Dockerfile -t pyscope-worker:1.1.0 .
# 运行只接受已经存在的镜像，并将 tag 解析成不可变的本地 image ID。
FROM python:3.12-slim
WORKDIR /opt/pyscope
COPY trace_worker.py profile_worker.py analysis_engine.py ./
COPY sandbox/analysis_worker.py ./analysis_worker.py
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["python3", "-I", "-B", "-u", "/opt/pyscope/trace_worker.py"]
