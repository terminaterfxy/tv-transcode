FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg mkvtoolnix \
      tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-eng \
      tesseract-ocr-jpn tesseract-ocr-kor \
      libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
# pgsrip 把 ISO 639-3 中文码 zho 直接交给 Tesseract；Debian 的模型文件却叫
# chi_sim.traineddata。补一个同目录别名，否则“中文包明明安装了”仍会 OCR 失败。
RUN set -eux; \
    chi_sim="$(find /usr/share/tesseract-ocr -name chi_sim.traineddata -print -quit)"; \
    test -n "$chi_sim"; \
    ln -sf "$(basename "$chi_sim")" "$(dirname "$chi_sim")/zho.traineddata"
RUN pip install --no-cache-dir \
      flask docker pgsrip==0.1.12 ffsubsync==0.5.0
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
