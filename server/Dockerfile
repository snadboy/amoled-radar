FROM python:3.12-slim

# fonts-dejavu-core is required: the status strip (temp/humidity) is rendered
# server-side, so the container needs a real TTF. curl is for HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core curl \
 && rm -rf /var/lib/apt/lists/*

# numpy + OpenCV for optical-flow in-between frames (the headless wheel needs no GUI libs);
# tzdata so per-city clocks never depend on what the slim base image happens to ship
# paho-mqtt: Home Assistant discovery + control (hub/mqtt.py)
RUN pip install --no-cache-dir "pillow>=10.2,<12" "numpy>=1.26,<3" "opencv-python-headless>=4.9,<5" tzdata "paho-mqtt>=2.1,<3"

WORKDIR /app
COPY hub ./hub

# The basemap never changes (fixed location), so it is fetched once and cached.
# Mount a volume here to keep it across restarts and avoid re-hitting Esri.
ENV HUB_CACHE=/cache RADAR_CACHE=/cache
VOLUME ["/cache"]

# 8080: devices (published on the LAN). 8081: admin page (DockTail VIP only).
EXPOSE 8080 8081
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["python3", "-u", "-m", "hub"]
