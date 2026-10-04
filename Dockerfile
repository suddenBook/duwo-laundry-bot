FROM python:3.12-slim

ENV TZ=Europe/Amsterdam
RUN ln -sf /usr/share/zoneinfo/Europe/Amsterdam /etc/localtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY duwo_monitor.py .
COPY tests ./tests
RUN python -m unittest discover -s tests

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF

CMD ["python", "-u", "duwo_monitor.py"]
