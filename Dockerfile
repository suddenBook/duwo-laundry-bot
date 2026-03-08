FROM python:3.12-slim

ENV TZ=Europe/Amsterdam
RUN ln -sf /usr/share/zoneinfo/Europe/Amsterdam /etc/localtime

WORKDIR /app

RUN pip install --no-cache-dir requests beautifulsoup4 urllib3 "qrcode[pil]"

COPY duwo_monitor.py .

CMD ["python", "-u", "duwo_monitor.py"]
