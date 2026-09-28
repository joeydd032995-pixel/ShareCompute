#include "llama.h"
#include "ggml-rpc.h"
#include "ggml-cpu.h"
#include "nlohmann/json.hpp"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <fstream>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>
using json = nlohmann::json;
using Clock = std::chrono::steady_clock;
static double ms(Clock::time_point a) { return std::chrono::duration<double, std::milli>(Clock::now()-a).count(); }
static void require(bool ok, const char * why) { if (!ok) throw std::runtime_error(why); }

int main(int argc, char ** argv) {
    if (argc == 2 && std::string(argv[1]) == "--version") { puts(SC_REVISION); return 0; }
    try {
        require(argc == 2, "usage: sc-split-probe CONFIG.json");
        std::ifstream in(argv[1]); json cfg; in >> cfg;
        auto endpoints = cfg.value("endpoints", std::vector<std::string>{});
        auto shares = cfg.value("shares", std::vector<float>{});
        int limit = cfg.value("tokens", 24);
        require(limit >= 1 && limit <= 128, "tokens must be 1..128");
        require(endpoints.size() <= 3 && endpoints.size() == shares.size(), "endpoint/share mismatch");
        require(std::set<std::string>(endpoints.begin(), endpoints.end()).size() == endpoints.size(), "duplicate endpoint");
        llama_log_set([](ggml_log_level, const char * text, void *) { fputs(text, stderr); }, nullptr);
        llama_backend_init();
        std::vector<ggml_backend_dev_t> devices;
        for (size_t i = 0; i < endpoints.size(); ++i) {
            require(endpoints[i].rfind("127.0.0.1:", 0) == 0 && shares[i] > 0, "RPC must use loopback tunnel and positive share");
            auto reg = ggml_backend_rpc_add_server(endpoints[i].c_str());
            require(reg && ggml_backend_reg_dev_count(reg) == 1, "worker must expose exactly one CPU");
            devices.push_back(ggml_backend_reg_dev_get(reg, 0));
            fprintf(stderr, "SC_DEVICE %zu %s\n", i, ggml_backend_dev_name(devices.back()));
        }
        devices.push_back(nullptr);
        auto params = llama_model_default_params();
        params.n_gpu_layers = endpoints.empty() ? 0 : 999;
        params.split_mode = LLAMA_SPLIT_MODE_LAYER;
        params.devices = devices.data();
        params.tensor_split = shares.empty() ? nullptr : shares.data();
        params.use_extra_bufts = false;
        auto start = Clock::now();
        auto model = llama_model_load_from_file(cfg.at("model").get<std::string>().c_str(), params);
        require(model != nullptr, "model load failed");
        auto vocab = llama_model_get_vocab(model);
        auto prompt = cfg.at("prompt").get<std::string>();
        std::vector<llama_token> input(prompt.size() + 32);
        int n = llama_tokenize(vocab, prompt.data(), (int)prompt.size(), input.data(), (int)input.size(), true, true);
        require(n > 0 && n <= 256, "prompt must contain 1..256 tokens"); input.resize(n);
        auto ctx_params = llama_context_default_params();
        ctx_params.n_ctx = 512; ctx_params.n_batch = 256; ctx_params.n_ubatch = 64;
        ctx_params.n_threads = 2; ctx_params.n_threads_batch = 2;
        auto ctx = llama_init_from_model(model, ctx_params); require(ctx != nullptr, "context creation failed");
        auto prefill = Clock::now();
        require(llama_decode(ctx, llama_batch_get_one(input.data(), n)) == 0, "prefill decode failed");
        double prefill_ms = ms(prefill);
        auto decode = Clock::now();
        auto sampler = llama_sampler_init_greedy();
        std::vector<llama_token> output; std::string text;
        for (int i = 0; i < limit; ++i) {
            auto token = llama_sampler_sample(sampler, ctx, -1);
            if (llama_vocab_is_eog(vocab, token)) break;
            output.push_back(token);
            std::vector<char> piece(256);
            int count = llama_token_to_piece(vocab, token, piece.data(), (int)piece.size(), 0, true);
            if (count < 0) { piece.resize(-count); count = llama_token_to_piece(vocab, token, piece.data(), (int)piece.size(), 0, true); }
            require(count >= 0, "token conversion failed"); text.append(piece.data(), count);
            if (i+1 < limit) require(llama_decode(ctx, llama_batch_get_one(&token, 1)) == 0, "generation decode failed");
        }
        require(!output.empty(), "empty generation");
        double decode_ms = ms(decode);
        llama_sampler_free(sampler); llama_free(ctx); llama_model_free(model); llama_backend_free();
        json result = {{"ok",true},{"token_ids",output},{"text",text},{"prompt_tokens",n},
                       {"prefill_ms",prefill_ms},{"decode_ms",decode_ms},{"wall_ms",ms(start)}};
        printf("SC_RESULT %s\n", result.dump(-1, ' ', false, json::error_handler_t::replace).c_str());
        return 0;
    } catch (const std::exception & e) { fprintf(stderr, "SC_FAILURE %s\n", e.what()); return 1; }
}
