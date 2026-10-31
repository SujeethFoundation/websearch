from services.search_service import tokens, cosine
def test_tokens(): assert tokens("Career Circle Week 3")==["career","circle","week","3"]
def test_cosine(): assert round(cosine([1,0],[1,0]),5)==1.0
