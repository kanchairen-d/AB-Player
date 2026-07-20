"""Simple test for get_play_url"""
import asyncio, sys
sys.path.insert(0, '/app')

async def test():
    from app.acfun import get_play_url
    url = await get_play_url('48686034', '39003529')
    print('type:', type(url).__name__)
    if url:
        print('starts with http:', url.startswith('http'))
        print('len:', len(url))
        print('first 60:', url[:60])
    else:
        print('None')

asyncio.run(test())