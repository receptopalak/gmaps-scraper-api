# Kazıma motoru: gosom/google-maps-scraper v1.11.1 (rod) + sıra numarası yaması.
#
# Neden: hazır gosom imajı işletme sayfalarını paralel açıp sonuçları bitme
# sırasıyla yazar; Google'daki liste sırası kaybolur. rank.patch, listedeki
# sırayı sayfalar açılmadan sabitler ve CSV'ye "rank" ve "sponsored"
# sütunlarını ekler. Daha önce kullanılan gosom/google-maps-scraper:latest-rod
# imajı da v1.11.1-rod'dur; geri kalan davranış değişmez.

ARG GOSOM_COMMIT=399c302f9682e1f182b0bc10a0e7bab302ed993a

FROM golang:1.26.2-trixie AS builder
ARG GOSOM_COMMIT
WORKDIR /app
RUN git init -q . \
    && git remote add origin https://github.com/gosom/google-maps-scraper.git \
    && git fetch -q --depth 1 origin "${GOSOM_COMMIT}" \
    && git checkout -q FETCH_HEAD
COPY rank.patch /tmp/rank.patch
RUN git apply --verbose /tmp/rank.patch
RUN go mod download
# Yama testleri geçmezse imaj derlenmez.
RUN go test -tags rod ./gmaps/
RUN CGO_ENABLED=0 go build -tags rod -ldflags="-w -s" -o /usr/bin/google-maps-scraper

# Rod için Chromium; sürüm go.mod'daki rod sürümüne sabit.
RUN go run github.com/go-rod/rod/lib/utils/get-browser

FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libasound2t64 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /var/tmp/* \
    && find /usr/share/doc -type f -delete \
    && find /usr/share/man -type f -delete \
    && find /usr/share/locale -type f -delete

COPY --from=builder /usr/bin/google-maps-scraper /usr/bin/
COPY --from=builder /root/.cache/rod /root/.cache/rod

ENTRYPOINT ["google-maps-scraper"]
