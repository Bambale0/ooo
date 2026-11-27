# Public search discovery

The public API origin is configured by `PUBLIC_API_BASE_URL`. Canonical pages are `/`, `/docs`, `/models`, and `/models/{slug}`; Russian is the default and the English home/docs variants use `?lang=en` with reciprocal hreflang. Existing price and guide aliases remain HTTP 200 for integration compatibility and declare the same canonical URLs.

`/robots.txt` links to `/sitemap.xml`. The sitemap includes only public, priced production model contracts and non-empty modality categories; private pages and non-public API responses use `X-Robots-Tag: noindex, nofollow`. Crawling is not authentication: private routes remain protected by their existing authorization, and crawlers can observe the noindex directive. The public model detail uses current partner price rows and never serializes supplier cost. Disabling a model removes its detail from sitemap and returns HTTP 404. Metadata and JSON-LD are server-rendered, and the canonical origin is never derived from request Host.

After deployment, check the sitemap, canonical URLs, inactive model 404s and cabinet robots header. Submit the sitemap in verified Google Search Console and Yandex Webmaster accounts; indexing and ranking depend on the search engines.
