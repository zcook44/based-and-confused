FROM python:3.13-alpine
RUN addgroup -S lab && adduser -S -G lab lab
COPY server.py /opt/server.py
USER lab
EXPOSE 8088
CMD ["python", "-u", "/opt/server.py"]
