FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg mkvtoolnix \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask docker
# dovi_tool（转换双层杜比视界用）：按机器架构取官方发行包，核对校验值后放进镜像。
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64) target=x86_64-unknown-linux-musl; sum=3933a4ea48970ba63dc007e1889791343ab8dea7f46492554cec5da5aad8c13e ;; \
      aarch64) target=aarch64-unknown-linux-musl; sum=d5ca2195f343828c5822926145b5c9c2f6c0124a432b1dff0c4b7a86f0674ce3 ;; \
      *) echo "unsupported architecture: $(uname -m)"; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/dovi_tool.tar.gz')" \
      "https://github.com/quietvoid/dovi_tool/releases/download/2.1.3/dovi_tool-2.1.3-${target}.tar.gz"; \
    echo "${sum}  /tmp/dovi_tool.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/tvtranscode; \
    tar -xzf /tmp/dovi_tool.tar.gz -C /opt/tvtranscode; \
    rm /tmp/dovi_tool.tar.gz; \
    chmod 755 /opt/tvtranscode/dovi_tool; \
    /opt/tvtranscode/dovi_tool --version
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8097
CMD ["python", "app.py"]
