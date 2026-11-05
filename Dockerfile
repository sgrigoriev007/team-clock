FROM python:3.12-slim
WORKDIR /app
COPY app.py ./
COPY static ./static
RUN mkdir /data && chown -R 10001:10001 /app /data
USER 10001:10001
ENV HOST=0.0.0.0 PORT=8000 DATABASE_PATH=/data/team-clock.sqlite3
EXPOSE 8000
CMD ["python", "app.py"]
