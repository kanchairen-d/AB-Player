FROM python:3.12-slim AS java-builder

# Install JDK
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    openjdk-21-jdk-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /compile

# Copy Java sources
COPY app/ABPlayerProxy.java app/ISpider.java /compile/
COPY app/stubs/ /compile/stubs/

# Compile
RUN mkdir -p out && \
    javac -d out -sourcepath stubs ABPlayerProxy.java ISpider.java && \
    echo "=== Compilation OK ==="

# Try to install Android SDK build-tools for DEX conversion
# Note: Debian's android-sdk-build-tools may not include d8/dx
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    dexdump \
    android-sdk-build-tools 2>/dev/null; \
    echo "=== Checking for dx/d8 ==="; \
    find /usr/lib/android-sdk -name "dx*" -o -name "d8*" 2>/dev/null; \
    echo "==="

# Create standard JAR
RUN cd out && jar cf /compile/abplayer.jar com/

# Attempt DEX conversion if dx/d8 available
RUN if command -v dx 2>/dev/null; then \
    dx --dex --output=/compile/abplayer_dex.jar /compile/out 2>&1 && \
    echo "=== DEX created with dx ==="; \
    elif command -v d8 2>/dev/null; then \
    d8 /compile/out --output /compile/abplayer_dex.jar 2>&1 && \
    echo "=== DEX created with d8 ==="; \
    else \
    echo "=== No DEX converter available, using standard JAR ==="; \
    fi

# Show results
RUN ls -la /compile/abplayer*.jar

# ===== Final stage =====
FROM python:3.12-slim

WORKDIR /app

# Install runtime Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app/ ./app/
COPY data/ ./data/

# Copy compiled JAR(s) from builder
COPY --from=java-builder /compile/abplayer*.jar /app/app/static/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]