#include "Hooks.h"
#include "JavaBridge.h"

#include <link.h>

#include <cctype>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>
#include <vector>

namespace modhost {
namespace {

struct Range { uintptr_t begin = 0, end = 0; };

std::map<std::string, uintptr_t> gFound; // event -> address (0 = not found)

// executable segments of libminecraftpe.so
std::vector<Range> gameRanges() {
    std::vector<Range> out;
    dl_iterate_phdr([](dl_phdr_info* info, size_t, void* data) -> int {
        if (!info->dlpi_name || !std::strstr(info->dlpi_name, "libminecraftpe.so")) return 0;
        auto* v = static_cast<std::vector<Range>*>(data);
        for (int i = 0; i < info->dlpi_phnum; ++i) {
            const ElfW(Phdr)& ph = info->dlpi_phdr[i];
            if (ph.p_type == PT_LOAD && (ph.p_flags & PF_X))
                v->push_back({info->dlpi_addr + ph.p_vaddr, info->dlpi_addr + ph.p_vaddr + ph.p_memsz});
        }
        return 1;
    }, &out);
    return out;
}

// "48 8B ?? 10" -> bytes + mask (false = wildcard)
bool parse(const std::string& text, std::vector<uint8_t>& bytes, std::vector<bool>& mask) {
    std::istringstream in(text);
    std::string tok;
    while (in >> tok) {
        if (tok == "?" || tok == "??") { bytes.push_back(0); mask.push_back(false); continue; }
        if (tok.size() != 2 || !std::isxdigit((unsigned char)tok[0]) || !std::isxdigit((unsigned char)tok[1])) return false;
        bytes.push_back(static_cast<uint8_t>(std::stoi(tok, nullptr, 16)));
        mask.push_back(true);
    }
    return !bytes.empty();
}

uintptr_t scan(const std::vector<Range>& ranges, const std::vector<uint8_t>& pat, const std::vector<bool>& mask) {
    for (const Range& r : ranges) {
        if (r.end - r.begin < pat.size()) continue;
        const uint8_t* base = reinterpret_cast<const uint8_t*>(r.begin);
        const size_t last = (r.end - r.begin) - pat.size();
        for (size_t i = 0; i <= last; ++i) {
            size_t j = 0;
            while (j < pat.size() && (!mask[j] || base[i + j] == pat[j])) ++j;
            if (j == pat.size()) return r.begin + i;
        }
    }
    return 0;
}

const std::pair<const char*, const char*> kBuiltIn[] = {
#include "Signatures.inc"
};

} // namespace

int Hooks::resolveAll(const std::filesystem::path& dir) {
    gFound.clear();
    std::error_code ec;
    std::vector<Range> ranges = gameRanges();
    std::map<std::string, std::string> sigs;
    for (auto& kv : kBuiltIn) sigs[kv.first] = kv.second;           // shipped patterns
    if (std::filesystem::exists(dir, ec))                           // files in <dataDir>/signatures override them
        for (auto& e : std::filesystem::directory_iterator(dir, ec)) {
            if (e.path().extension() != ".txt") continue;
            std::ifstream f(e.path());
            std::stringstream ss;
            ss << f.rdbuf();
            sigs[e.path().stem().string()] = ss.str();
        }
    int ok = 0;
    for (auto& kv : sigs) {
        std::vector<uint8_t> bytes;
        std::vector<bool> mask;
        uintptr_t addr = 0;
        if (!ranges.empty() && parse(kv.second, bytes, mask)) addr = scan(ranges, bytes, mask);
        gFound[kv.first] = addr;
        if (addr) ++ok;
    }
    return ok;
}

uintptr_t Hooks::address(const std::string& event) const {
    auto it = gFound.find(event);
    return it == gFound.end() ? 0 : it->second;
}

std::string Hooks::status() const {
    std::string s;
    for (auto& kv : gFound) s += "\n" + kv.first + (kv.second ? "=ok" : "=missing");
    if (gFound.empty()) s += "\n(no signatures)";
    return s;
}

Hooks& hooks() { static Hooks h; return h; }

} // namespace modhost

// ---------------------------------------------------------------------------------------------
// Detours. Installed with the preloader SDK (pl::memory::hook), so this part only builds inside the template.
//
// Every hook uses the same hand-written arm64 thunk: it saves ALL argument registers (x0-x8, q0-q7), raises the
// event, restores the registers and jumps into the original function. The thunk never looks at an argument, so
// it is correct whatever the function's real signature is. Events fire just BEFORE the game function runs.
#if __has_include(<pl/memory/Hook.hpp>) && defined(__aarch64__)
#include <pl/memory/Hook.hpp>

#include <atomic>
#include <chrono>

#define MH_EVENTS(X)                                                                                              \
    X(0, "world.join") X(1, "world.leave") X(2, "player.login") X(3, "player.tick") X(4, "level.tick")           \
    X(5, "game.update") X(6, "block.break") X(7, "block.place") X(8, "mob.kill") X(9, "player.die")              \
    X(10, "mob.interact") X(11, "item.use") X(12, "item.drop") X(13, "item.craft") X(14, "player.teleport")      \
    X(15, "chat.send") X(16, "chat.display") X(17, "player.attack")

extern "C" {
void* modhost_orig[32];
void modhost_hook_fired(int id);
}

#define MH_STR2(x) #x
#define MH_STR(x) MH_STR2(x)
#define MH_THUNK(ID, NAME)                                                                                        \
    extern "C" __attribute__((naked)) void modhost_thunk_##ID() {                                                 \
        asm volatile(                                                                                             \
            "stp x29, x30, [sp, #-224]!\n"                                                                        \
            "mov x29, sp\n"                                                                                       \
            "stp x0, x1, [sp, #16]\n stp x2, x3, [sp, #32]\n stp x4, x5, [sp, #48]\n stp x6, x7, [sp, #64]\n"      \
            "str x8, [sp, #80]\n"                                                                                 \
            "stp q0, q1, [sp, #96]\n stp q2, q3, [sp, #128]\n stp q4, q5, [sp, #160]\n stp q6, q7, [sp, #192]\n"  \
            "mov w0, #" MH_STR(ID) "\n"                                                                           \
            "bl modhost_hook_fired\n"                                                                             \
            "ldp q6, q7, [sp, #192]\n ldp q4, q5, [sp, #160]\n ldp q2, q3, [sp, #128]\n ldp q0, q1, [sp, #96]\n"  \
            "ldr x8, [sp, #80]\n"                                                                                 \
            "ldp x6, x7, [sp, #64]\n ldp x4, x5, [sp, #48]\n ldp x2, x3, [sp, #32]\n ldp x0, x1, [sp, #16]\n"      \
            "ldp x29, x30, [sp], #224\n"                                                                          \
            "adrp x16, modhost_orig\n"                                                                            \
            "add x16, x16, :lo12:modhost_orig\n"                                                                  \
            "ldr x16, [x16, #(" MH_STR(ID) "*8)]\n"                                                               \
            "br x16\n");                                                                                          \
    }
MH_EVENTS(MH_THUNK)

namespace modhost {
namespace {

const char* const kNames[] = {
#define MH_NAME(ID, NAME) NAME,
    MH_EVENTS(MH_NAME)
#undef MH_NAME
};
constexpr int kCount = sizeof(kNames) / sizeof(kNames[0]);
void* const kThunks[] = {
#define MH_PTR(ID, NAME) reinterpret_cast<void*>(&modhost_thunk_##ID),
    MH_EVENTS(MH_PTR)
#undef MH_PTR
};

std::vector<void*> gTargets(kCount, nullptr);
std::vector<bool> gOn(kCount, false);
std::atomic<bool> gFirst[kCount];
thread_local bool tInside = false;

} // namespace

int Hooks::install() {
    int n = 0;
    for (int i = 0; i < kCount; ++i) {
        if (gOn[i]) { ++n; continue; }
        uintptr_t a = address(kNames[i]);
        if (!a) continue;
        void* orig = nullptr;
        if (pl::memory::hook(reinterpret_cast<void*>(a), kThunks[i], &orig) != 0 || !orig) continue;
        modhost_orig[i] = orig; gTargets[i] = reinterpret_cast<void*>(a); gOn[i] = true; ++n;
    }
    return n;
}

void Hooks::uninstall() {
    // events stop at once; the patched functions keep jumping through their thunks, which just forward
    for (int i = 0; i < kCount; ++i) gOn[i] = false;
}

} // namespace modhost

extern "C" void modhost_hook_fired(int id) {
    using namespace modhost;
    if (id < 0 || id >= kCount || !gOn[id] || tInside) return;
    if (id == 5) { // game.update runs every frame: pass at most 20 per second to Java
        static std::atomic<long long> last{0};
        long long now = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count();
        if (now - last.load() < 50) return;
        last.store(now);
    }
    tInside = true;
    if (!gFirst[id].exchange(true)) bridge().fire("hook.first", kNames[id]);
    bridge().fire(kNames[id], "");
    tInside = false;
}
#else
namespace modhost {
int Hooks::install() { return 0; }
void Hooks::uninstall() {}
} // namespace modhost
#endif
