FROM python:3.12-slim
# ssh — для доступа к серверам; Node и Claude Code — только для /analyze (можно убрать, если /analyze не нужен)
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates curl nodejs npm \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /bot
COPY bot.py run.sh ./
COPY ops ./ops
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "bot.py"]
