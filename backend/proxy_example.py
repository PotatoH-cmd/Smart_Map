from fastapi import FastAPI, Response
import httpx

app = FastAPI()
BASE = 'https://gis95.yskc.com/server/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM/MapServer/tile'

@app.get('/proxy/gf-tiles/{z}/{y}/{x}')
async def gf_tiles(z: int, y: int, x: int):
    url = f"{BASE}/{z}/{y}/{x}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': '*',
        'Content-Type': r.headers.get('Content-Type', 'image/png'),
        'Cache-Control': 'public, max-age=600',
    }
    return Response(content=r.content, headers=headers, status_code=r.status_code)
