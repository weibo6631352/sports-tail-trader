// C++ RSS leak PoC: 模拟跟 Rust PoC 同样工作负载
// - 每 2s GET gamma /events?live=true
// - 用 macOS 自带 libcurl (LibreSSL TLS, 跟 Python httpx 的 OpenSSL 类似)
// - 无 jemalloc, 用 macOS 系统 malloc — 对比 Python 是否仅是"语言层 GC"问题, 还是 TLS lib

#include <iostream>
#include <chrono>
#include <thread>
#include <curl/curl.h>
#include <string>
#include <unistd.h>

const char* URL = "https://gamma-api.polymarket.com/events?limit=50&active=true&closed=false";

static size_t write_cb(void* contents, size_t size, size_t nmemb, void* userp) {
    auto* s = static_cast<std::string*>(userp);
    s->append(static_cast<char*>(contents), size * nmemb);
    return size * nmemb;
}

int main() {
    std::cout << "cpp_poc start pid=" << getpid() << " (libcurl + system malloc)" << std::endl;
    curl_global_init(CURL_GLOBAL_DEFAULT);
    CURL* curl = curl_easy_init();
    if (!curl) { std::cerr << "curl init failed\n"; return 1; }

    curl_easy_setopt(curl, CURLOPT_URL, URL);
    curl_easy_setopt(curl, CURLOPT_HTTP_VERSION, CURL_HTTP_VERSION_2_0);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "gzip");
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 10L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_cb);

    int tick = 0;
    while (true) {
        std::string buffer;
        buffer.reserve(4 * 1024 * 1024);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &buffer);
        auto t0 = std::chrono::steady_clock::now();
        CURLcode res = curl_easy_perform(curl);
        auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::steady_clock::now() - t0).count();
        ++tick;
        if (tick % 15 == 0) {
            std::cout << "tick=" << tick << " bytes=" << buffer.size()
                      << " ms=" << ms << " res=" << res << std::endl;
        }
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
}
