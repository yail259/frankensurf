from __future__ import annotations
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from frankensurf import Runtime, WebPolicy

async def run(args):
    manifest=json.loads(Path(args.manifest).read_text())
    output=Path(args.output)
    output.mkdir(parents=True,exist_ok=True)
    results=[]
    for provider in args.providers:
        async with Runtime(output / ("private-state-"+provider),steel_api_url=args.steel_url,domain_delay=0.5) as web:
            for case in manifest["cases"]:
                request_url=case["url"] if provider=="http" else case["rendered_url"]
                adapter=case["adapter"] if provider=="http" else case["rendered_adapter"]
                result=await web.extract(request_url,adapter,WebPolicy(provider=provider,freshness="now",include_images=True,max_images=3,timeout_seconds=20))
                item={"case_id":case["id"],"provider":provider,"url":request_url,"representation":adapter,"historical_listing_state":case["historical_listing_state"],"historical_note":case["note"],"current_listing_state":"unknown","receipt":result["receipt"],"title":result["title"],"image_url_count":len(result["image_urls"]),"images":result["images"]}
                structured=result.get("structured")
                if isinstance(structured,dict):
                    product=structured.get("product",structured)
                    variants=product.get("variants")
                    if isinstance(variants,list):
                        item["current_merchant_variant_claims"]=[{k:v.get(k) for k in ("id","title","price","available")} for v in variants]
                    products=structured.get("products")
                    if isinstance(products,list):
                        item["catalogue_product_count"]=len(products)
                        item["catalogue_variant_count"]=sum(len(p.get("variants",[])) for p in products if isinstance(p,dict))
                results.append(item)
                (output / "results.json").write_text(json.dumps({"schema":"frankensurf.identity-sprint-eval/v1","generated_at":datetime.now(timezone.utc).isoformat(),"scope":manifest["scope"],"results":results},indent=2)+"\n")
                print(json.dumps({"case_id":case["id"],"provider":provider,"status":result["receipt"]["status"],"failure":result["receipt"].get("failure"),"latency_ms":result["receipt"]["latency_ms"],"decoded_images":sum(i["status"]=="decoded" for i in result["images"])}),flush=True)
    totals={provider:{"attempted":sum(r["provider"]==provider for r in results),"observed":sum(r["provider"]==provider and r["receipt"]["status"]=="observed" for r in results),"decoded_images":sum(i["status"]=="decoded" for r in results if r["provider"]==provider for i in r["images"])} for provider in args.providers}
    (output / "summary.json").write_text(json.dumps({"totals":totals,"paid_spend_usd":0,"caveat":"These are live retrieval outcomes. Historical source status is not current verified state; identity tests use a separate controlled fixture."},indent=2)+"\n")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--manifest",default=str(Path(__file__).with_name("identity-benchmark.json")))
    p.add_argument("--output",required=True)
    p.add_argument("--providers",nargs="+",default=["http","steel"])
    p.add_argument("--steel-url",default="http://127.0.0.1:3100")
    asyncio.run(run(p.parse_args()))
