# gmaps-scraper-api

Google Maps sonuçlarını job tabanlı şekilde toplayan bir REST API servisidir. Bu servis, [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) üzerine kuruludur ve scraping işini arka planda yürütür. Sen istemci tarafında sadece job oluşturur, durumu kontrol eder ve sonuçları alırsın.

Bu README, uygulamaya nasıl istek atacağını, hangi endpoint'in ne işe yaradığını, hangi parametrelere hangi değerleri verebileceğini ve hangi cevapları beklemen gerektiğini detaylı şekilde anlatır.

## İçindekiler

- [Genel Akış](#genel-akış)
- [Base URL](#base-url)
- [Swagger ve OpenAPI](#swagger-ve-openapi)
- [Hızlı Başlangıç](#hızlı-başlangıç)
- [Neler Yapabilirsin](#neler-yapabilirsin)
- [API Özeti](#api-özeti)
- [1. Job Oluşturma](#1-job-oluşturma)
- [2. Tek Job Durumu Sorgulama](#2-tek-job-durumu-sorgulama)
- [3. Sonucu Dosya Olarak Alma](#3-sonucu-dosya-olarak-alma)
- [4. Sonucu Direkt JSON Olarak Alma](#4-sonucu-direkt-json-olarak-alma)
- [5. Bugünkü Tüm Job'ları Listeleme](#5-bugünkü-tüm-jobları-listeleme)
- [Parametreler](#parametreler)
- [Response Alanları](#response-alanları)
- [Job Durumları](#job-durumları)
- [Hata Kodları ve Anlamları](#hata-kodları-ve-anlamları)
- [Yaygın Kullanım Senaryoları](#yaygın-kullanım-senaryoları)
- [JavaScript Örnekleri](#javascript-örnekleri)
- [Python Örnekleri](#python-örnekleri)
- [Deduplication Davranışı](#deduplication-davranışı)
- [Sistem Davranışı ve Limitler](#sistem-davranışı-ve-limitler)
- [Ortam Değişkenleri](#ortam-değişkenleri)
- [Yerel Geliştirme](#yerel-geliştirme)
- [Dosya Yapısı](#dosya-yapısı)

## Genel Akış

Bu API senkron çalışmaz. Yani `POST /api/jobs` attığında scraping bitene kadar beklemez. Bunun yerine bir `job_id` üretir ve arka planda scraping başlar.

Akış şu şekildedir:

```text
1. Client -> POST /api/jobs
2. API -> job_id döner
3. API arka planda Gosom'u poll eder
4. İş bitince CSV indirir, JSON'a çevirir, diske kaydeder
5. Client -> GET /api/jobs/{job_id}
6. Status "ok" olduğunda client -> /result veya /result/json çağırır
```

Kısaca:

- Job oluşturursun
- `job_id` alırsın
- Durum sorgularsın
- Sonucu indirirsin veya direkt JSON olarak alırsın

## Base URL

Production örneği:

```text
https://scraper.geolocalrank.com
```

Local örneği:

```text
http://localhost:8003
```

Bu README’de örneklerde `BASE_URL` değişkeni kullanılmıştır:

```bash
BASE_URL=https://scraper.geolocalrank.com
```

Local çalışıyorsan:

```bash
BASE_URL=http://localhost:8003
```

## Swagger ve OpenAPI

Canlı dokümantasyon:

```text
https://scraper.geolocalrank.com/docs
```

OpenAPI şeması:

```text
https://scraper.geolocalrank.com/openapi.json
```

Swagger arayüzü endpoint’leri denemek için faydalıdır, ama gerçek entegrasyon için aşağıdaki örnekler daha nettir.

## Hızlı Başlangıç

En kısa kullanım akışı:

```bash
BASE_URL=https://scraper.geolocalrank.com

curl -X POST "$BASE_URL/api/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"query":"bakery istanbul","depth":1,"max_reviews":5}'
```

Örnek cevap:

```json
{
  "job_id": "a7a7d3ef-3119-4c83-80db-233484e2838d",
  "status": "pending"
}
```

Sonra:

```bash
curl "$BASE_URL/api/jobs/a7a7d3ef-3119-4c83-80db-233484e2838d"
```

İş bittiğinde:

```bash
curl "$BASE_URL/api/jobs/a7a7d3ef-3119-4c83-80db-233484e2838d/result/json"
```

## Neler Yapabilirsin

Bu API ile şunları yapabilirsin:

- Google Maps'te bir anahtar kelime veya işletme araması başlatmak
- Aynı sorguyu tekrar attığında cache/dedup sayesinde mevcut sonucu almak
- Bir job'ın tamamlanıp tamamlanmadığını kontrol etmek
- Sonucu dosya gibi indirmek
- Sonucu direkt JSON body olarak almak
- Bugün açılmış tüm job'ları listelemek

Örnek sorgular:

- `restaurants istanbul`
- `bakery kadikoy`
- `dentist ankara`
- `cafes izmir`
- `villamore trabzon`
- `avukat beşiktaş`

Çok replica'lı deployment notu:

- Sonuç JSON'u artık Redis'te tutulur ve tüm pod'lar aynı sonucu buradan servis eder
- Disk yazımı sadece backup amaçlıdır
- Bu sayede rollout, pod restart veya 2+ replica senaryolarında sonuçlar tek pod diskine bağımlı kalmaz

## API Özeti

| Method | Endpoint | Açıklama |
|---|---|---|
| `POST` | `/api/jobs` | Yeni scraping job'ı oluşturur |
| `GET` | `/api/jobs` | Bugün oluşturulan tüm job'ları listeler |
| `GET` | `/api/jobs/{job_id}` | Tek job'ın durumunu döner |
| `GET` | `/api/jobs/{job_id}/result` | Sonucu dosya indirme davranışıyla döner |
| `GET` | `/api/jobs/{job_id}/result/json` | Sonucu direkt JSON olarak döner |

## 1. Job Oluşturma

Endpoint:

```text
POST /api/jobs
```

Amaç:

- Yeni bir scraping işi başlatmak
- Aynı sorgu bugün daha önce çalıştıysa mevcut job'ı kullanmak

### Request Body

```json
{
  "query": "restaurants istanbul",
  "depth": 1,
  "max_reviews": 10,
  "extra_reviews": false,
  "place_id": null,
  "lang": "tr",
  "geo": null,
  "zoom": null,
  "radius": null,
  "email": false,
  "fast_mode": false
}
```

### Parametre Açıklamaları

| Alan | Tip | Zorunlu | Varsayılan | Geçerli Aralık | Açıklama |
|---|---|---|---|---|---|
| `query` | string | Evet | Yok | Boş olamaz | Google Maps arama sorgusu. `place_id` kullanıyorsan ilgili yerin adını burada da göndermelisin |
| `depth` | integer | Hayır | `1` | `1-10` | Kaç seviye/kaç sayfa derine gidileceği |
| `max_reviews` | integer | Hayır | `10` | `0-500` | Her işletme için maksimum review sayısı |
| `extra_reviews` | boolean | Hayır | `false` | `true/false` | Gosom'un geniş review toplama modunu denemek için |
| `place_id` | string | Hayır | `null` | string | Deneysel: Google Maps place ID ile arama denemesi. Tek başına kullanılmaz, ilgili yerin adı `query` içinde de verilmelidir |
| `lang` | string | Hayır | `tr` | örn: `tr`, `en`, `de` | Sonuç dili |
| `geo` | string | Hayır | `null` | `lat,lng` | Aramayı belirli koordinata göre yönlendirme |
| `zoom` | integer | Hayır | `null` | `0-21` | Harita zoom seviyesi |
| `radius` | number | Hayır | `null` | `> 0` | Arama yarıçapı, metre cinsinden |
| `email` | boolean | Hayır | `false` | `true/false` | İşletme websitesinden email toplamayı dener |
| `fast_mode` | boolean | Hayır | `false` | `true/false` | Daha hızlı ama daha az detaylı scraping denemesi |

### `query` İçin Ne Yazılır

`query`, Google Maps'te manuel aratabileceğin metindir.

Doğru örnekler:

```text
restaurants istanbul
bakery beşiktaş
dentist ankara
hotel trabzon
villamore trabzon
```

Dikkat:

- Şehir eklemek çoğu zaman daha doğru sonuç verir
- Semt veya ilçe eklemek sonucu netleştirir
- Marka veya işletme adını tek başına da kullanabilirsin

### `depth` Ne İşe Yarar

`depth`, aramanın ne kadar derine gideceğini belirler.

Genel yaklaşım:

- `1`: hızlı ve hafif sorgular
- `2-3`: daha geniş kapsama
- `4+`: daha yavaş ama daha fazla sonuç potansiyeli

Örnek:

```json
{
  "query": "cafes istanbul",
  "depth": 3,
  "max_reviews": 20
}
```

### `max_reviews` Ne İşe Yarar

Her işletme için toplanacak review sayısını sınırlar.

Örnek kullanım:

- `0`: sadece işletme verisi, review istemiyorsan
- `10`: hafif kullanım
- `50`: orta seviye veri
- `100+`: daha ağır iş yükü

Not:

- `max_reviews` üst sınırdır, garanti edilen sayı değildir
- Google Maps tarafında görünürlük, scraping davranışı ve provider limitleri nedeniyle daha az review dönebilir
- Daha fazla review için `extra_reviews: true` denemelisin

### `extra_reviews` Ne İşe Yarar

Bu alan `true` olduğunda Gosom'un geniş review toplama modunu açmayı dener.

Örnek:

```json
{
  "query": "restaurants istanbul",
  "depth": 1,
  "max_reviews": 15,
  "extra_reviews": true
}
```

Beklenti:

- Review sayısı artabilir
- Ama yine de `15` garanti edilmez
- Bu alan upstream davranışına bağlıdır

### `place_id` Ne İşe Yarar

Bu alan deneysel olarak place ID üzerinden job oluşturmayı dener.

Örnek:

```json
{
  "place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4",
  "query": "Google Sydney",
  "depth": 1,
  "max_reviews": 20,
  "extra_reviews": true
}
```

Not:

- Bu özellik Gosom tarafında resmi olarak dokümante edilmiş değildir
- `place_id` gönderiyorsan aynı istekte ilgili yerin adını `query` içinde de göndermelisin
- Wrapper, place ID'den Google Maps URL üretip upstream'e keyword olarak gönderir
- Çalışırsa kalır, güvenilmez davranırsa kaldırılmalıdır

### `lang` Ne İşe Yarar

Arama ve veri toplama dilini belirler.

Örnek:

```json
{
  "query": "restaurants istanbul",
  "lang": "en"
}
```

Kullanım örnekleri:

- `tr`
- `en`
- `de`

### `geo` Ne İşe Yarar

Aramayı belirli koordinata yakınlaştırmak için kullanılır.

Format:

```text
lat,lng
```

Örnek:

```json
{
  "query": "restaurants",
  "geo": "41.0082,28.9784"
}
```

### `zoom` Ne İşe Yarar

Harita zoom seviyesini belirler.

- minimum: `0`
- maksimum: `21`

Örnek:

```json
{
  "query": "cafes istanbul",
  "zoom": 14
}
```

### `radius` Ne İşe Yarar

Arama yarıçapını metre cinsinden belirler.

Örnek:

```json
{
  "query": "dentist ankara",
  "radius": 2500
}
```

### `email` Ne İşe Yarar

İşletmenin websitesi üzerinden email adresi toplamayı dener.

Örnek:

```json
{
  "query": "lawyer istanbul",
  "email": true
}
```

Not:

- Email her işletmede bulunmaz
- Website olmayan kayıtlarda doğal olarak boş dönebilir

### `fast_mode` Ne İşe Yarar

Upstream scraper'ın daha hızlı ama daha düşük detaylı çalışma modunu dener.

Örnek:

```json
{
  "query": "bakery istanbul",
  "fast_mode": true
}
```

### Başarılı Yeni Job Cevabı

HTTP `201`

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "status": "pending"
}
```

### Aynı Sorgu Daha Önce Varsa

Eğer aynı gün içinde aynı `query + depth + max_reviews` kombinasyonu zaten oluşturulduysa servis yeni job açmayabilir.

Tamamlanmış job örneği:

HTTP `200`

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "status": "ok",
  "result_count": 20,
  "download_url": "/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result",
  "result_url": "/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result/json"
}
```

Henüz bitmemiş job örneği:

HTTP `200`

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "query": "restaurants istanbul",
  "place_id": null,
  "status": "pending"
}
```

### cURL Örneği

```bash
curl -X POST "$BASE_URL/api/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"query":"restaurants istanbul","depth":1,"max_reviews":10}'
```

### Olası Hatalar

- `400`: body yanlış veya eksik
- `409`: aynı sorgu için job oluşturulurken race condition oluştu
- `502`: Gosom tarafına ulaşılamadı veya job açılamadı
- `503`: job açıldı ama Redis'e kaydedilemedi

## 2. Tek Job Durumu Sorgulama

Endpoint:

```text
GET /api/jobs/{job_id}
```

Amaç:

- Tek bir job'ın şu anki durumunu öğrenmek
- Sonuç hazırsa `download_url` ve `result_url` almak

### Örnek İstek

```bash
curl "$BASE_URL/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48"
```

### `pending` Cevabı

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "query": "restaurants istanbul",
  "place_id": null,
  "status": "pending",
  "created_at": "2026-03-09T14:28:36.061990",
  "extra_reviews": false,
  "lang": "tr",
  "geo": null,
  "zoom": null,
  "radius": null,
  "email": false,
  "fast_mode": false
}
```

### `ok` Cevabı

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "query": "restaurants istanbul",
  "place_id": null,
  "status": "ok",
  "created_at": "2026-03-09T14:28:36.061990",
  "extra_reviews": true,
  "lang": "tr",
  "geo": "41.0082,28.9784",
  "zoom": 14,
  "radius": 2500,
  "email": true,
  "fast_mode": false,
  "result_count": 20,
  "download_url": "/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result",
  "result_url": "/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result/json"
}
```

### `failed` Cevabı

```json
{
  "job_id": "97c9ed6c-7e52-4c74-aa34-d18a16b9dd48",
  "query": "restaurants istanbul",
  "place_id": null,
  "status": "failed",
  "created_at": "2026-03-09T14:28:36.061990",
  "extra_reviews": false,
  "lang": "tr",
  "geo": null,
  "zoom": null,
  "radius": null,
  "email": false,
  "fast_mode": false,
  "error": "CSV indirme hatası: timeout"
}
```

### Olası Hatalar

- `400`: `job_id` formatı geçersiz
- `404`: job bulunamadı

## 3. Sonucu Dosya Olarak Alma

Endpoint:

```text
GET /api/jobs/{job_id}/result
```

Amaç:

- Sonucu dosya gibi almak
- Tarayıcı veya client bunu indirme davranışıyla ele alabilir

### Örnek İstek

```bash
curl "$BASE_URL/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result"
```

Dosyaya yazmak istersen:

```bash
curl "$BASE_URL/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result" -o result.json
```

### Ne Döner

- Job bitmişse JSON içeriği
- Job henüz bitmemişse `202`
- Job başarısızsa `422`
- Sonuç dosyası silinmişse `410`

## 4. Sonucu Direkt JSON Olarak Alma

Endpoint:

```text
GET /api/jobs/{job_id}/result/json
```

Amaç:

- Sonucu doğrudan JSON response olarak almak
- API entegrasyonlarında çoğu zaman tercih edilen endpoint budur

### Örnek İstek

```bash
curl "$BASE_URL/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result/json"
```

Biçimlendirilmiş görmek için:

```bash
curl -s "$BASE_URL/api/jobs/97c9ed6c-7e52-4c74-aa34-d18a16b9dd48/result/json" | python -m json.tool
```

### Başarılı Cevap Örneği

```json
[
  {
    "title": "Cafe Example",
    "category": "Cafe",
    "address": "Istanbul",
    "phone": "+90 ...",
    "website": "https://example.com",
    "review_count": 120,
    "review_rating": 4.6,
    "latitude": 41.0082,
    "longitude": 28.9784
  }
]
```

### Olası Hatalar

- `202`: job henüz tamamlanmadı
- `400`: `job_id` formatı geçersiz
- `404`: job bulunamadı
- `410`: sonuç dosyası yok
- `422`: job başarısız

## 5. Bugünkü Tüm Job'ları Listeleme

Endpoint:

```text
GET /api/jobs
```

Amaç:

- Bugün oluşturulmuş tüm job'ları görmek
- Toplu takip yapmak

### Örnek İstek

```bash
curl "$BASE_URL/api/jobs"
```

### Örnek Cevap

```json
[
  {
    "job_id": "11111111-1111-1111-1111-111111111111",
    "query": "restaurants istanbul",
    "status": "pending",
    "created_at": "2026-03-12T11:40:13.249794"
  },
  {
    "job_id": "22222222-2222-2222-2222-222222222222",
    "query": "dentist ankara",
    "status": "ok",
    "created_at": "2026-03-12T10:15:00.000000",
    "result_count": 15,
    "download_url": "/api/jobs/22222222-2222-2222-2222-222222222222/result",
    "result_url": "/api/jobs/22222222-2222-2222-2222-222222222222/result/json"
  },
  {
    "job_id": "33333333-3333-3333-3333-333333333333",
    "query": "hotel trabzon",
    "status": "failed",
    "created_at": "2026-03-12T09:00:00.000000",
    "error": "CSV indirme hatası: timeout"
  }
]
```

Not:

- Bu endpoint sadece bugünkü job'ları döner
- Geçmiş günlerin sonuçları burada görünmeyebilir

## Parametreler

### `query`

- Tip: `string`
- Zorunlu: evet
- Boş olamaz

Örnekler:

```text
restaurants istanbul
bakery kadikoy
hotel trabzon
villamore trabzon
```

### `depth`

- Tip: `integer`
- Zorunlu: hayır
- Varsayılan: `1`
- Min: `1`
- Max: `10`

Öneri:

- Basit kullanım için `1`
- Daha geniş sonuç için `2` veya `3`
- Gereksiz yere çok yükseltme, iş süresini artırır

### `max_reviews`

- Tip: `integer`
- Zorunlu: hayır
- Varsayılan: `10`
- Min: `0`
- Max: `500`

Öneri:

- Hafif kullanım için `0-10`
- Normal kullanım için `10-50`
- Ağır kullanım için `100+`

## Response Alanları

| Alan | Ne zaman gelir | Açıklama |
|---|---|---|
| `job_id` | Her zaman | Job kimliği |
| `query` | Job detay cevaplarında | Arama sorgusu |
| `status` | Her zaman | `pending`, `ok`, `failed` |
| `created_at` | Job detay/listede | Oluşturulma zamanı, ISO format |
| `result_count` | Sadece `ok` durumunda | Sonuç sayısı |
| `download_url` | Sadece `ok` durumunda | Dosya gibi sonuç alma endpoint'i |
| `result_url` | Sadece `ok` durumunda | Direkt JSON alma endpoint'i |
| `error` | Sadece `failed` durumunda | Hata detayı |
| `extra_reviews` | Job response'larında | Geniş review modu açık mı |
| `place_id` | Job response'larında | Deneysel place ID değeri |
| `lang` | Job response'larında | Kullanılan dil |
| `geo` | Job response'larında | Kullanılan koordinat |
| `zoom` | Job response'larında | Kullanılan zoom |
| `radius` | Job response'larında | Kullanılan radius |
| `email` | Job response'larında | Email toplama denendi mi |
| `fast_mode` | Job response'larında | Hızlı mod açık mı |

## Job Durumları

| Durum | Anlamı |
|---|---|
| `pending` | Job oluşturuldu, scraping sürüyor |
| `ok` | Job tamamlandı, sonuç hazır |
| `failed` | İş başarısız oldu |

## Hata Kodları ve Anlamları

| Kod | Nerede | Anlamı |
|---|---|---|
| `200` | GET ve dedupe POST | Başarılı |
| `201` | Yeni POST | Yeni job oluşturuldu |
| `202` | `/result` ve `/result/json` | Job henüz tamamlanmadı |
| `400` | Birçok endpoint | Geçersiz input veya `job_id` |
| `404` | Job endpoint'leri | Job bulunamadı |
| `409` | `POST /api/jobs` | Aynı sorgu için job o anda oluşturuluyor |
| `410` | Result endpoint'leri | Sonuç dosyası silinmiş |
| `422` | Result endpoint'leri | Job başarısız durumda |
| `502` | `POST /api/jobs` | Gosom tarafında hata |
| `503` | `POST /api/jobs` | Redis kayıt problemi |

## Yaygın Kullanım Senaryoları

### Senaryo 1: Yeni job oluştur, tamamlanana kadar bekle, sonucu al

```bash
BASE_URL=https://scraper.geolocalrank.com

JOB_ID=$(curl -s -X POST "$BASE_URL/api/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"query":"villamore trabzon","depth":1,"max_reviews":10}' | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "JOB_ID=$JOB_ID"

while true; do
  STATUS=$(curl -s "$BASE_URL/api/jobs/$JOB_ID" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "STATUS=$STATUS"
  [ "$STATUS" != "pending" ] && break
  sleep 3
done

curl -s "$BASE_URL/api/jobs/$JOB_ID/result/json" | python -m json.tool
```

### Senaryo 2: Aynı sorguyu tekrar at

```bash
curl -X POST "$BASE_URL/api/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"query":"villamore trabzon","depth":1,"max_reviews":10}'
```

Eğer aynı gün içinde aynı istek daha önce tamamlandıysa çoğu zaman doğrudan `status: ok` ile mevcut job döner.

### Senaryo 3: Sonucu dosya olarak indir

```bash
curl "$BASE_URL/api/jobs/$JOB_ID/result" -o result.json
```

### Senaryo 4: Tüm bugünkü job'ları listele

```bash
curl "$BASE_URL/api/jobs" | python -m json.tool
```

## JavaScript Örnekleri

### Fetch ile job oluşturma

```js
const baseUrl = "https://scraper.geolocalrank.com";

const response = await fetch(`${baseUrl}/api/jobs`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    query: "restaurants istanbul",
    depth: 1,
    max_reviews: 10,
  }),
});

const data = await response.json();
console.log(data);
```

### Fetch ile polling

```js
const baseUrl = "https://scraper.geolocalrank.com";

async function waitForJob(jobId) {
  while (true) {
    const res = await fetch(`${baseUrl}/api/jobs/${jobId}`);
    const data = await res.json();

    if (data.status === "ok") {
      return data;
    }

    if (data.status === "failed") {
      throw new Error(data.error || "Job failed");
    }

    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}
```

### Fetch ile sonucu alma

```js
const resultRes = await fetch(`${baseUrl}/api/jobs/${jobId}/result/json`);

if (resultRes.status === 202) {
  console.log("Henüz hazır değil");
} else if (!resultRes.ok) {
  throw new Error(`HTTP ${resultRes.status}`);
} else {
  const result = await resultRes.json();
  console.log(result);
}
```

## Python Örnekleri

### `requests` ile kullanım

```python
import time
import requests

BASE_URL = "https://scraper.geolocalrank.com"

create_res = requests.post(
    f"{BASE_URL}/api/jobs",
    json={
        "query": "restaurants istanbul",
        "depth": 1,
        "max_reviews": 10,
    },
    timeout=30,
)
create_res.raise_for_status()
job = create_res.json()
job_id = job["job_id"]

while True:
    status_res = requests.get(f"{BASE_URL}/api/jobs/{job_id}", timeout=30)
    status_res.raise_for_status()
    status_data = status_res.json()

    if status_data["status"] == "ok":
        break

    if status_data["status"] == "failed":
        raise RuntimeError(status_data.get("error", "Job failed"))

    time.sleep(3)

result_res = requests.get(f"{BASE_URL}/api/jobs/{job_id}/result/json", timeout=120)
result_res.raise_for_status()
data = result_res.json()
print(data)
```

## Deduplication Davranışı

Bu servis aynı gün içinde aynı sorguyu tekrar tekrar çalıştırmamak için deduplication yapar.

Dedup key şu alanlardan oluşur:

- `query`
- `depth`
- `max_reviews`
- bugünün tarihi

Bunun anlamı:

- Bugün aynı sorguyu tekrar atarsan yeni job açılmayabilir
- Yarın aynı sorgu atılırsa yeni job açılabilir
- `depth` veya `max_reviews` değişirse yeni job oluşur

Örnek:

```json
{"query":"bakery istanbul","depth":1,"max_reviews":10}
```

ile

```json
{"query":"bakery istanbul","depth":2,"max_reviews":10}
```

aynı job sayılmaz.

## Sistem Davranışı ve Limitler

Bilmen gereken bazı noktalar:

- Servis scraping işini senkron değil asenkron yapar
- Sonuçlar öncelikle Redis'te tutulur
- Disk yazımı sadece backup amaçlı yapılır
- Eski klasörler temizlenir
- Sonuçlar bugüne göre indexlenir
- `job_id` UUID formatındadır
- Çok yoğun eşzamanlı trafikte aynı sorgu için `409` alabilirsin

Polling davranışı:

- 1 saniyeden başlar
- her turda 0.5 saniye artar
- maksimum 3 saniyeye kadar çıkar
- en fazla 720 poll yapılır

Yaklaşık timeout:

- yaklaşık 36 dakika

Çoklu replica davranışı:

- `2+` replica ile çalışabilir
- Sonuç endpoint'leri dosyayı yerel pod diskinde aramak zorunda değildir
- Bir pod job'ı tamamladığında diğer pod'lar da sonucu Redis üzerinden dönebilir
- Disk backup kaybolsa bile Redis TTL süresi boyunca sonuç servis edilmeye devam eder

## Ortam Değişkenleri

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `GOSOM_API_URL` | `http://localhost:8080/api/v1` | Gosom servis adresi |
| `DATA_ROOT` | `gmapsdata/json` | Sonuç JSON dosyalarının klasörü |
| `REDIS_URL` | `redis://localhost:6379/0` veya türetilen değer | Redis bağlantı adresi |
| `REDIS_HOST` | `localhost` | `REDIS_URL` yoksa kullanılır |
| `REDIS_PORT` | `6379` | `REDIS_URL` yoksa kullanılır |
| `REDIS_DB` | `0` | `REDIS_URL` yoksa kullanılır |

Kubernetes örneği:

```yaml
env:
  - name: REDIS_URL
    value: redis://:supersecretredis@redis-master.databases.svc.cluster.local:6379/0
```

## Yerel Geliştirme

### Docker Compose ile

```bash
git clone https://github.com/receptopalak/gmaps-scraper-api.git
cd gmaps-scraper-api
docker-compose up -d --build
```

Servisler:

- Gosom scraper: `http://localhost:8085`
- Wrapper API: `http://localhost:8003`

### Docker olmadan wrapper çalıştırma

```bash
pip install -r requirements.txt
uvicorn wrapper:app --host 0.0.0.0 --port 8000 --reload
```

Not:

- Gosom ve Redis ayrıca erişilebilir olmalı
- Environment variable'lar doğru set edilmeli

## Dosya Yapısı

```text
gmaps-scraper-api/
  wrapper.py
  Dockerfile
  docker-compose.yml
  requirements.txt
  tests/
  gmapsdata/
    json/
      YYYY-MM-DD/
        index.json
        <job-id>.json
```

Klasör anlamları:

- `wrapper.py`: FastAPI uygulaması
- `docker-compose.yml`: local orkestrasyon
- `gmapsdata/json/YYYY-MM-DD/`: sonuçların saklandığı klasör
- `index.json`: o günün job kayıtlarının yedeği

## Tam Örnek Akış

```bash
BASE_URL=https://scraper.geolocalrank.com

# 1. Job oluştur
JOB_ID=$(curl -s -X POST "$BASE_URL/api/jobs" \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"restaurants istanbul",
    "depth":1,
    "max_reviews":15,
    "extra_reviews":true,
    "lang":"tr",
    "geo":"41.0082,28.9784",
    "zoom":14,
    "radius":2500,
    "email":true,
    "fast_mode":false
  }' | python -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

echo "JOB_ID=$JOB_ID"

# 2. Durumu izle
while true; do
  RESP=$(curl -s "$BASE_URL/api/jobs/$JOB_ID")
  STATUS=$(echo "$RESP" | python -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "$RESP"
  [ "$STATUS" != "pending" ] && break
  sleep 3
done

# 3. Sonucu direkt JSON olarak al
curl -s "$BASE_URL/api/jobs/$JOB_ID/result/json" | python -m json.tool

# 4. Sonucu dosya olarak indir
curl "$BASE_URL/api/jobs/$JOB_ID/result" -o result.json

# 5. Bugünkü tüm job'ları listele
curl -s "$BASE_URL/api/jobs" | python -m json.tool
```

## Lisans

MIT
