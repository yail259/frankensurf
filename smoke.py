import asyncio
import json
from pathlib import Path
from frankensurf import Runtime, WebPolicy

ROOT = Path(__file__).parent
CASES = [
    ("static", "https://example.com", None),
    ("closed-auction", "https://www.allbids.com.au/c/sports-recreation-fitness/bikes-scooters/ebike-1555877", None),
    ("cash-bike", "https://www.cashconverters.com.au/shop/outdoor-sports/outdoor-recreation/bicycles/bike/037900378113", None),
    ("gumtree-xds", "https://www.gumtree.com.au/web/listing/men-s-bicycles/1344697847", "#__NEXT_DATA__"),
]

async def main():
    results = []
    # Each provider is an independent controlled comparison, never a bypass retry chain.
    for provider in ["steel", "local", "http"]:
        async with Runtime(ROOT/"results"/provider,steel_api_url="http://127.0.0.1:3100") as web:
            for label,url,selector in CASES:
                result=await web.read(url, WebPolicy(provider=provider,wait_selector=selector if provider != "http" else None,timeout_seconds=20))
                result.pop("content",None)
                result["case"]=label
                results.append(result)
                (ROOT/"results"/"smoke-partial.json").write_text(json.dumps(results,ensure_ascii=False,indent=2))
                print(provider,label,result["receipt"]["status"],result["receipt"]["failure"],result["receipt"]["latency_ms"],len(result["image_urls"]),flush=True)
    async with Runtime(ROOT/"results"/"structured") as web:
        results.extend(await web.batch([
            "https://thebicycleexchange.com.au/collections/bikes/products.json?limit=30",
            "https://99bikes.com.au/products/pedal-breeze-st-electric-cruiser-bike-satin-flare-black.js",
        ], WebPolicy(provider="http",include_images=True,max_images=8),adapter="json"))
    report={"observations":results,"paid_provider_cost_usd":0,"steel_image":"ghcr.io/steel-dev/steel-browser@sha256:f5cd68fbc2cb27e5d7766269860fd0fb29cbe5fe506245a49c82f85ba210e7da","caveats":["One trial per URL/provider; not a statistical success-rate estimate","Listing availability remains unknown without source-specific verification","Page DOM image URLs may include recommendations/logos; decoded bytes are separate evidence","No cloud proxies or challenge solvers used"]}
    (ROOT/"results"/"smoke.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print("SAVED",ROOT/"results"/"smoke.json",flush=True)

asyncio.run(main())
