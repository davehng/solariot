FROM python:3.12-slim

WORKDIR /solariot

RUN groupadd -g 2000 solariot && useradd -m -u 2000 -g solariot solariot
COPY requirements.txt /solariot/
RUN pip install --no-cache-dir --upgrade -r requirements.txt
COPY . /solariot/

USER solariot
ENV PYTHONPATH="/config"
CMD ["python3", "solariot.py", "-v"]
