import httpx
import time

URL = "http://localhost:9903/v1/embeddings"
BATCH_SIZE = 16
NUM_BATCHES = 5
TEXT = "Testing RTX 5080 acceleration with Qwen3 GGUF."

def test():
    texts = [f"{TEXT} {i}" for i in range(BATCH_SIZE)]
    payload = {"input": texts, "model": "qwen3"}
    
    print(f"Testing throughput on {URL} (RTX 5080)...")
    
    latencies = []
    total_t0 = time.perf_counter()
    
    try:
        with httpx.Client(timeout=30.0) as client:
            # Warmup
            client.post(URL, json=payload)
            
            for b in range(NUM_BATCHES):
                t0 = time.perf_counter()
                resp = client.post(URL, json=payload)
                t1 = time.perf_counter()
                if resp.status_code == 200:
                    latencies.append(t1 - t0)
                    print(f"Batch {b+1}: {t1-t0:.3f}s")
                else:
                    print(f"Error: {resp.text}")
                    return
    except Exception as e:
        print(f"Failed: {e}")
        return

    total_time = time.perf_counter() - total_t0
    avg_latency = sum(latencies) / len(latencies)
    throughput = (BATCH_SIZE * NUM_BATCHES) / total_time
    
    print(f"\nRTX 5080 Results (Qwen3 Q8):")
    print(f"Avg Batch Latency: {avg_latency:.3f}s")
    print(f"Throughput: {throughput:.2f} embeddings/sec (Total Time)")

if __name__ == "__main__":
    test()
