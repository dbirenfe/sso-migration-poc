FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

USER 1001
EXPOSE 8080

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "4", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "proxy:app"]
