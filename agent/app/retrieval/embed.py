"""向量嵌入通道。

与 `app/llm/` 完全同构：定义一个抽象、给一个 OpenAI 兼容实现、
未配置时装一个空实现由调用方降级。之所以照抄那套结构，是因为
《需求分析说明书》7.2 定的原则是「接口层抽象、不绑定厂商」，
嵌入没有理由破例。

**为什么是 OpenAI 兼容的 `/v1/embeddings`**：本地部署的三种主流方式
（LM Studio、Ollama、vLLM）和几乎所有云厂商都实现了这个端点，
一个实现覆盖全部场景，换模型只改 `.env` 里的两行。

    EMBED_BASE_URL=http://127.0.0.1:1234/v1     # LM Studio 默认端口
    EMBED_MODEL=text-embedding-bge-m3

注意 **DeepSeek 不提供 embeddings 接口**，所以嵌入的地址与密钥是独立于
`LLM_*` 的一组配置，不能复用同一个 base_url。团队的大模型仍然是 DeepSeek，
嵌入走本地模型，两者互不影响。

三条工程约束：

1. **嵌入不可用不影响功能。** 拿不到向量就只用词法通道，检索照常返回结果。
   这不是「优雅降级」的场面话——项目全部自动化测试就跑在没有嵌入的模式下。
2. **语料侧向量落盘缓存。** 语料只有几十条，算一次存起来，
   之后启动不再请求；语料变了按内容哈希自动失效，不需要手动清缓存。
3. **查询侧不缓存。** 用户问什么无法预知，缓存命中率接近零，
   反而要处理淘汰。查询侧嵌入失败就直接关掉这一路。
"""

import hashlib
import json
import math
import os
import urllib.error
import urllib.request


class EmbeddingUnavailable(Exception):
    """嵌入服务不可用。调用方应捕获并降级到纯词法检索。"""


class EmbeddingProvider(object):
    """所有嵌入实现的公共接口。"""

    name = "base"
    available = True

    def embed(self, texts):
        """``[str] -> [[float]]``。不可用时抛 `EmbeddingUnavailable`。"""
        raise NotImplementedError


class NullEmbedding(EmbeddingProvider):
    """未配置嵌入时的空实现。"""

    name = "null"
    available = False

    def embed(self, texts):
        raise EmbeddingUnavailable("未配置嵌入服务，当前只使用词法检索")


class OpenAICompatibleEmbedding(EmbeddingProvider):
    """兼容 OpenAI `/v1/embeddings` 协议的实现，只用标准库 urllib。

    用 urllib 而不是 httpx 有个实打实的理由：httpx 对着 LM Studio
    的本地端点会返回 502，urllib 不会。这是踩过的坑，不是偏好。
    """

    name = "openai_compatible"

    def __init__(self, base_url, model, api_key="", timeout=30.0, batch_size=32):
        self._base_url = (base_url or "").rstrip("/")
        self._model = model or "text-embedding-3-small"
        # 本地部署通常不校验密钥，留空是正常情况
        self._api_key = api_key or "not-needed"
        self._timeout = timeout
        self._batch_size = batch_size

    def embed(self, texts):
        if not self._base_url:
            raise EmbeddingUnavailable("未配置 EMBED_BASE_URL")
        texts = [t if t else " " for t in texts]

        vectors = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _embed_batch(self, batch):
        payload = {"model": self._model, "input": batch}
        request = urllib.request.Request(
            self._base_url + "/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._api_key},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()
            raise EmbeddingUnavailable("嵌入服务返回 HTTP %s" % exc.code)
        except Exception as exc:
            raise EmbeddingUnavailable(
                "嵌入服务连接失败（%s）" % type(exc).__name__)

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingUnavailable("嵌入服务返回格式异常")
        # 按 index 排序：协议允许乱序返回，实测大多数实现是有序的，
        # 但一旦乱序而我们又不排，向量会张冠李戴，且**不会报错**——
        # 只会让检索结果莫名其妙地差，是最难查的那类问题。
        try:
            data = sorted(data, key=lambda item: item.get("index", 0))
            return [list(item["embedding"]) for item in data]
        except (KeyError, TypeError):
            raise EmbeddingUnavailable("嵌入服务返回格式异常")


def build_embedder(settings):
    """按配置构造嵌入实现。未配置或配置不全时返回 `NullEmbedding`。"""
    if not getattr(settings, "embed_base_url", ""):
        return NullEmbedding()
    return OpenAICompatibleEmbedding(
        base_url=settings.embed_base_url,
        model=settings.embed_model,
        api_key=settings.embed_api_key,
        timeout=settings.embed_timeout)


# ---------------------------------------------------------------- 向量运算

def normalize_vector(vector):
    """归一化到单位长度，让余弦相似度退化成点积。

    检索时每次查询要跟几十条语料比一遍，先归一化能省掉重复开方。
    零向量原样返回，避免除零。
    """
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return list(vector)
    return [v / norm for v in vector]


def cosine(a, b):
    """两个**已归一化**向量的余弦相似度。长度不等时返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------- 缓存

def text_key(text):
    """语料片的缓存键：内容哈希。

    用内容而不是片 id 做键，语料改了缓存自动失效，
    不会出现「FAQ 正文改了但向量还是旧的」这种只影响效果、
    不产生任何报错的静默故障。
    """
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


class VectorCache(object):
    """语料向量的磁盘缓存。

    存成 JSON 而不是二进制：几十条向量的体积无所谓，
    而可读的文本格式能进 git diff，评审时能看出「向量文件变了」。
    """

    def __init__(self, path):
        self.path = path
        self.model = ""
        self.vectors = {}
        self._dirty = False

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return self
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.model = data.get("model", "")
            self.vectors = data.get("vectors", {}) or {}
        except (ValueError, OSError):
            # 缓存坏了就当没有，重新算，不要让一个缓存文件把服务拖垮
            self.model, self.vectors = "", {}
        return self

    def get(self, text, model):
        """模型不一致时视为未命中——不同模型的向量空间不可混用。"""
        if model and self.model and model != self.model:
            return None
        return self.vectors.get(text_key(text))

    def put(self, text, vector):
        self.vectors[text_key(text)] = vector
        self._dirty = True

    def save(self, model=""):
        if not self.path or not self._dirty:
            return False
        self.model = model or self.model
        try:
            # **裸文件名时 dirname 是空串，而 os.makedirs("") 抛
            # FileNotFoundError**——它是 OSError 的子类，会被下面的
            # except 静默吞掉，于是缓存永远存不下来，每次进程重启都要把
            # 整个语料重新嵌入一遍（实测约 195 次请求、4 秒）。
            # 而这个失败没有任何迹象：save() 返回 False，没人看返回值。
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump({"model": self.model, "vectors": self.vectors},
                          handle, ensure_ascii=False)
            self._dirty = False
            return True
        except OSError:
            # 只读目录下跑（比如打包成只读镜像）不该导致服务不可用
            return False
