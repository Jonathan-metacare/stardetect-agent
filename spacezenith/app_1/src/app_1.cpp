#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <ctime>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>
#include <condition_variable>

namespace {

volatile sig_atomic_t g_stop_requested = 0;

void on_sigterm(int) { g_stop_requested = 1; }

struct Config {
  std::string agent_base_url = "http://127.0.0.1:8001";
  int connect_timeout_ms = 3000;
  int request_timeout_ms = 60000;
  std::size_t max_image_bytes = 8U * 1024U * 1024U;
  std::size_t max_prompt_bytes = 64U * 1024U;
  std::string docker_path = "docker";
  std::string llm_container = "llm-qwen3-vl";
  std::string agent_container = "stardetect-agent";
  std::string llm_health_url = "http://127.0.0.1:8003/v1/models";
  std::string agent_health_url = "http://127.0.0.1:8001/health";
  int backend_start_timeout_ms = 180000;
  int backend_poll_interval_ms = 1000;
};

struct Endpoint {
  std::string host;
  std::string port;
  std::string base_path;
};

struct HttpResponse {
  int status = 0;
  std::string body;
};

struct AppError : std::runtime_error {
  std::string code;

  AppError(std::string error_code, const std::string& message)
      : std::runtime_error(message), code(std::move(error_code)) {}
};

constexpr std::size_t kTelemetryFrameSize = 1066U;
constexpr int kTelemetryIntervalMs = 500;
constexpr int kTelemetryIoTimeoutMs = 200;

enum class TelemetryStatus : std::uint8_t {
  kRunning = 0x00,
  kSuccess = 0x01,
  kFailure = 0x02,
  kCancelled = 0x03,
};

#pragma pack(push, 1)
struct InnerTeleFrame {
  std::uint8_t source_device;
  std::uint8_t cmd;
  std::uint32_t length_le;
  std::array<std::uint8_t, 1060> data;
};
#pragma pack(pop)

static_assert(sizeof(InnerTeleFrame) == kTelemetryFrameSize,
              "InnerTeleFrame must match the platform's 1066-byte wire format");

std::uint8_t parse_device_code(const std::string& value) {
  if (value.empty() || !std::all_of(value.begin(), value.end(), [](unsigned char ch) {
        return std::isdigit(ch) != 0;
      })) {
    throw AppError("invalid_device_code", "argv[9] must be a decimal device code from 0 to 255");
  }
  std::size_t consumed = 0;
  unsigned long parsed = 0;
  try {
    parsed = std::stoul(value, &consumed, 10);
  } catch (const std::exception&) {
    throw AppError("invalid_device_code", "argv[9] must be a decimal device code from 0 to 255");
  }
  if (consumed != value.size() || parsed > 255U) {
    throw AppError("invalid_device_code", "argv[9] must be a decimal device code from 0 to 255");
  }
  return static_cast<std::uint8_t>(parsed);
}

std::uint32_t to_little_endian(std::uint32_t value) {
  const std::uint16_t probe = 1;
  if (*reinterpret_cast<const std::uint8_t*>(&probe) == 1U) {
    return value;
  }
  return ((value & 0x000000FFU) << 24U) | ((value & 0x0000FF00U) << 8U) |
         ((value & 0x00FF0000U) >> 8U) | ((value & 0xFF000000U) >> 24U);
}

class TelemetryReporter {
 public:
  TelemetryReporter(std::string server_path, std::uint8_t device_code)
      : server_path_(std::move(server_path)), device_code_(device_code) {}

  TelemetryReporter(const TelemetryReporter&) = delete;
  TelemetryReporter& operator=(const TelemetryReporter&) = delete;

  ~TelemetryReporter() {
    stop_worker();
    close_socket();
  }

  void start() {
    if (server_path_.empty()) {
      std::cerr << "warning: telemetry socket path (argv[8]) is empty; telemetry disabled\n";
      return;
    }
    worker_ = std::thread([this] { run(); });
  }

  void finish(TelemetryStatus status) {
    stop_worker();
    if (!server_path_.empty()) {
      send_status(status);
    }
    close_socket();
  }

 private:
  void run() {
    std::unique_lock<std::mutex> lock(mutex_);
    while (!stopping_) {
      lock.unlock();
      send_status(TelemetryStatus::kRunning);
      lock.lock();
      wakeup_.wait_for(lock, std::chrono::milliseconds(kTelemetryIntervalMs),
                       [this] { return stopping_; });
    }
  }

  void stop_worker() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    wakeup_.notify_all();
    if (worker_.joinable()) {
      worker_.join();
    }
  }

  void close_socket() {
    std::lock_guard<std::mutex> lock(socket_mutex_);
    if (socket_fd_ >= 0) {
      close(socket_fd_);
      socket_fd_ = -1;
    }
  }

  bool connect_socket() {
    if (server_path_.size() >= sizeof(sockaddr_un::sun_path)) {
      std::cerr << "warning: telemetry socket path is too long: " << server_path_ << '\n';
      return false;
    }
    const int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
      std::cerr << "warning: cannot create telemetry socket: " << std::strerror(errno) << '\n';
      return false;
    }
    const int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
      std::cerr << "warning: cannot configure telemetry socket: " << std::strerror(errno) << '\n';
      close(fd);
      return false;
    }

    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::memcpy(address.sun_path, server_path_.c_str(), server_path_.size() + 1U);
    const socklen_t address_length = static_cast<socklen_t>(offsetof(sockaddr_un, sun_path) +
                                                            server_path_.size() + 1U);
    if (connect(fd, reinterpret_cast<const sockaddr*>(&address), address_length) != 0) {
      if (errno != EINPROGRESS) {
        std::cerr << "warning: cannot connect telemetry socket " << server_path_ << ": "
                  << std::strerror(errno) << '\n';
        close(fd);
        return false;
      }
      pollfd descriptor{fd, POLLOUT, 0};
      const int poll_result = poll(&descriptor, 1, kTelemetryIoTimeoutMs);
      int socket_error = 0;
      socklen_t socket_error_size = sizeof(socket_error);
      if (poll_result <= 0 || getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error,
                                         &socket_error_size) != 0 || socket_error != 0) {
        std::cerr << "warning: telemetry socket connection timed out or failed\n";
        close(fd);
        return false;
      }
    }
    socket_fd_ = fd;
    return true;
  }

  bool send_status(TelemetryStatus status) {
    std::lock_guard<std::mutex> lock(socket_mutex_);
    if (socket_fd_ < 0 && !connect_socket()) {
      return false;
    }
    InnerTeleFrame frame{};
    frame.source_device = device_code_;
    frame.cmd = 0x00;
    frame.length_le = to_little_endian(1U);
    frame.data[0] = static_cast<std::uint8_t>(status);

    std::size_t offset = 0;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(kTelemetryIoTimeoutMs);
    while (offset < sizeof(frame)) {
      const auto now = std::chrono::steady_clock::now();
      if (now >= deadline) {
        std::cerr << "warning: telemetry frame send timed out\n";
        close(socket_fd_);
        socket_fd_ = -1;
        return false;
      }
      const int timeout = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(
          deadline - now).count());
      pollfd descriptor{socket_fd_, POLLOUT, 0};
      if (poll(&descriptor, 1, std::max(1, timeout)) <= 0 ||
          (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
        std::cerr << "warning: telemetry socket became unavailable\n";
        close(socket_fd_);
        socket_fd_ = -1;
        return false;
      }
      const auto* bytes = reinterpret_cast<const std::uint8_t*>(&frame);
      const ssize_t sent = send(socket_fd_, bytes + offset, sizeof(frame) - offset, MSG_NOSIGNAL);
      if (sent > 0) {
        offset += static_cast<std::size_t>(sent);
      } else if (sent < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
        continue;
      } else {
        std::cerr << "warning: telemetry frame send failed: " << std::strerror(errno) << '\n';
        close(socket_fd_);
        socket_fd_ = -1;
        return false;
      }
    }
    return true;
  }

  std::string server_path_;
  std::uint8_t device_code_;
  int socket_fd_ = -1;
  std::mutex mutex_;
  std::mutex socket_mutex_;
  std::condition_variable wakeup_;
  bool stopping_ = false;
  std::thread worker_;
};

std::string trim(std::string value) {
  const auto first = std::find_if_not(value.begin(), value.end(), [](unsigned char ch) {
    return std::isspace(ch) != 0;
  });
  const auto last = std::find_if_not(value.rbegin(), value.rend(), [](unsigned char ch) {
    return std::isspace(ch) != 0;
  }).base();
  return first >= last ? "" : std::string(first, last);
}

int parse_positive_int(const std::string& value, const std::string& name) {
  std::size_t consumed = 0;
  long parsed = 0;
  try {
    parsed = std::stol(value, &consumed, 10);
  } catch (const std::exception&) {
    throw AppError("invalid_config", name + " must be a positive integer");
  }
  if (consumed != value.size() || parsed <= 0 || parsed > std::numeric_limits<int>::max()) {
    throw AppError("invalid_config", name + " must be a positive integer");
  }
  return static_cast<int>(parsed);
}

Config load_config(const std::filesystem::path& path) {
  Config config;
  std::ifstream input(path);
  if (!input) {
    std::cout << "config not found, using defaults: " << path << '\n';
    return config;
  }

  std::string line;
  unsigned int line_number = 0;
  while (std::getline(input, line)) {
    ++line_number;
    line = trim(line);
    if (line.empty() || line.front() == '#') {
      continue;
    }
    const auto separator = line.find('=');
    if (separator == std::string::npos) {
      throw AppError("invalid_config", "invalid config line " + std::to_string(line_number));
    }
    const std::string key = trim(line.substr(0, separator));
    const std::string value = trim(line.substr(separator + 1));
    if (key == "agent_base_url") {
      config.agent_base_url = value;
    } else if (key == "connect_timeout_ms") {
      config.connect_timeout_ms = parse_positive_int(value, key);
    } else if (key == "request_timeout_ms") {
      config.request_timeout_ms = parse_positive_int(value, key);
    } else if (key == "max_image_bytes") {
      config.max_image_bytes = static_cast<std::size_t>(parse_positive_int(value, key));
    } else if (key == "max_prompt_bytes") {
      config.max_prompt_bytes = static_cast<std::size_t>(parse_positive_int(value, key));
    } else if (key == "docker_path") {
      config.docker_path = value;
    } else if (key == "llm_container") {
      config.llm_container = value;
    } else if (key == "agent_container") {
      config.agent_container = value;
    } else if (key == "llm_health_url") {
      config.llm_health_url = value;
    } else if (key == "agent_health_url") {
      config.agent_health_url = value;
    } else if (key == "backend_start_timeout_ms") {
      config.backend_start_timeout_ms = parse_positive_int(value, key);
    } else if (key == "backend_poll_interval_ms") {
      config.backend_poll_interval_ms = parse_positive_int(value, key);
    } else {
      throw AppError("invalid_config", "unknown config key: " + key);
    }
  }
  if (config.docker_path.empty() || config.llm_container.empty() || config.agent_container.empty() ||
      config.llm_health_url.empty() || config.agent_health_url.empty()) {
    throw AppError("invalid_config", "backend container configuration must not be empty");
  }
  return config;
}

Endpoint parse_endpoint(const std::string& url) {
  constexpr std::string_view prefix = "http://";
  if (url.rfind(prefix, 0) != 0) {
    throw AppError("invalid_config", "agent_base_url must start with http://");
  }
  const std::string remainder = url.substr(prefix.size());
  const auto slash = remainder.find('/');
  const std::string authority = remainder.substr(0, slash);
  if (authority.empty()) {
    throw AppError("invalid_config", "agent_base_url host is empty");
  }

  Endpoint endpoint;
  endpoint.base_path = slash == std::string::npos ? "" : remainder.substr(slash);
  if (endpoint.base_path == "/") {
    endpoint.base_path.clear();
  }

  if (authority.front() == '[') {
    const auto closing = authority.find(']');
    if (closing == std::string::npos) {
      throw AppError("invalid_config", "invalid IPv6 agent_base_url");
    }
    endpoint.host = authority.substr(1, closing - 1);
    endpoint.port = closing + 1 < authority.size() && authority[closing + 1] == ':'
                        ? authority.substr(closing + 2)
                        : "80";
  } else {
    const auto colon = authority.rfind(':');
    if (colon != std::string::npos && authority.find(':') == colon) {
      endpoint.host = authority.substr(0, colon);
      endpoint.port = authority.substr(colon + 1);
    } else {
      endpoint.host = authority;
      endpoint.port = "80";
    }
  }
  if (endpoint.host.empty() || endpoint.port.empty()) {
    throw AppError("invalid_config", "agent_base_url host or port is empty");
  }
  return endpoint;
}

std::string json_escape(const std::string& value) {
  std::ostringstream escaped;
  for (const unsigned char ch : value) {
    switch (ch) {
      case '"': escaped << "\\\""; break;
      case '\\': escaped << "\\\\"; break;
      case '\b': escaped << "\\b"; break;
      case '\f': escaped << "\\f"; break;
      case '\n': escaped << "\\n"; break;
      case '\r': escaped << "\\r"; break;
      case '\t': escaped << "\\t"; break;
      default:
        if (ch < 0x20U) {
          escaped << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                  << static_cast<unsigned int>(ch) << std::dec << std::setfill(' ');
        } else {
          escaped << static_cast<char>(ch);
        }
    }
  }
  return escaped.str();
}

std::string base64_encode(const std::vector<std::uint8_t>& bytes) {
  static constexpr char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string encoded;
  encoded.reserve(((bytes.size() + 2U) / 3U) * 4U);
  for (std::size_t index = 0; index < bytes.size(); index += 3U) {
    const std::uint32_t first = bytes[index];
    const std::uint32_t second = index + 1U < bytes.size() ? bytes[index + 1U] : 0U;
    const std::uint32_t third = index + 2U < bytes.size() ? bytes[index + 2U] : 0U;
    const std::uint32_t triple = (first << 16U) | (second << 8U) | third;
    encoded.push_back(alphabet[(triple >> 18U) & 0x3FU]);
    encoded.push_back(alphabet[(triple >> 12U) & 0x3FU]);
    encoded.push_back(index + 1U < bytes.size() ? alphabet[(triple >> 6U) & 0x3FU] : '=');
    encoded.push_back(index + 2U < bytes.size() ? alphabet[triple & 0x3FU] : '=');
  }
  return encoded;
}

unsigned int parse_preset_index(const std::string& value) {
  std::size_t consumed = 0;
  unsigned long parsed = 0;
  try {
    parsed = std::stoul(value, &consumed, 10);
  } catch (const std::exception&) {
    throw AppError("invalid_preset", "argv[2] must be a decimal preset number from 1 to 255");
  }
  if (consumed != value.size() || parsed == 0 || parsed > 255U) {
    throw AppError("invalid_preset", "argv[2] must be a decimal preset number from 1 to 255");
  }
  return static_cast<unsigned int>(parsed);
}

std::filesystem::path select_image_preset(const std::filesystem::path& raw_dir,
                                          unsigned int preset_id) {
  const std::string stem = "image" + std::to_string(preset_id);
  for (const char* extension : {".jpg", ".jpeg", ".png"}) {
    const std::filesystem::path candidate = raw_dir / (stem + extension);
    std::error_code error;
    if (std::filesystem::is_regular_file(candidate, error) && !error) {
      return candidate;
    }
  }
  throw AppError("preset_not_found", "image preset not found under raw/: " + stem);
}

std::string read_prompt(const std::filesystem::path& raw_dir, unsigned int preset_id,
                        std::size_t max_prompt_bytes, std::filesystem::path& prompt_path) {
  prompt_path = raw_dir / ("prompt" + std::to_string(preset_id) + ".txt");
  std::error_code error;
  const auto size = std::filesystem::file_size(prompt_path, error);
  if (error || size == 0U) {
    throw AppError("preset_not_found", "prompt preset is missing or empty: " + prompt_path.string());
  }
  if (size > max_prompt_bytes) {
    throw AppError("prompt_too_large", "prompt preset exceeds max_prompt_bytes");
  }
  std::ifstream input(prompt_path, std::ios::binary);
  if (!input) {
    throw AppError("input_open_failed", "cannot open prompt preset: " + prompt_path.string());
  }
  std::string prompt(static_cast<std::size_t>(size), '\0');
  input.read(prompt.data(), static_cast<std::streamsize>(prompt.size()));
  if (input.gcount() != static_cast<std::streamsize>(prompt.size())) {
    throw AppError("input_read_failed", "cannot read complete prompt preset: " + prompt_path.string());
  }
  if (g_stop_requested != 0) {
    throw AppError("cancelled", "SIGTERM received while reading prompt preset");
  }
  return prompt;
}

std::pair<std::string, std::vector<std::uint8_t>> read_image(
    const std::filesystem::path& path, std::size_t max_image_bytes) {
  std::error_code error;
  const auto size = std::filesystem::file_size(path, error);
  if (error || size == 0U) {
    throw AppError("invalid_image", "CH1 image is missing or empty: " + path.string());
  }
  if (size > max_image_bytes) {
    throw AppError("image_too_large", "CH1 image exceeds max_image_bytes");
  }

  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw AppError("input_open_failed", "cannot open CH1 image read-only: " + path.string());
  }
  std::vector<std::uint8_t> bytes(static_cast<std::size_t>(size));
  input.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
  if (input.gcount() != static_cast<std::streamsize>(bytes.size())) {
    throw AppError("input_read_failed", "cannot read complete CH1 image: " + path.string());
  }
  if (g_stop_requested != 0) {
    throw AppError("cancelled", "SIGTERM received while reading image");
  }

  const bool jpeg = bytes.size() >= 3U && bytes[0] == 0xFFU && bytes[1] == 0xD8U &&
                    bytes[2] == 0xFFU;
  const bool png = bytes.size() >= 8U && bytes[0] == 0x89U && bytes[1] == 0x50U &&
                   bytes[2] == 0x4EU && bytes[3] == 0x47U && bytes[4] == 0x0DU &&
                   bytes[5] == 0x0AU && bytes[6] == 0x1AU && bytes[7] == 0x0AU;
  if (!jpeg && !png) {
    throw AppError("unsupported_image", "CH1 image must be JPEG or PNG");
  }
  return {jpeg ? "image/jpeg" : "image/png", std::move(bytes)};
}

int remaining_ms(const std::chrono::steady_clock::time_point& deadline) {
  const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
      deadline - std::chrono::steady_clock::now()).count();
  if (remaining <= 0) {
    return 0;
  }
  return remaining > std::numeric_limits<int>::max() ? std::numeric_limits<int>::max()
                                                       : static_cast<int>(remaining);
}

void wait_for_fd(int fd, short events, const std::chrono::steady_clock::time_point& deadline) {
  while (true) {
    if (g_stop_requested != 0) {
      throw AppError("cancelled", "SIGTERM received during Agent request");
    }
    const int timeout = remaining_ms(deadline);
    if (timeout == 0) {
      throw AppError("request_timeout", "Agent request timed out");
    }
    pollfd descriptor{fd, events, 0};
    const int poll_result = poll(&descriptor, 1, std::min(timeout, 100));
    if (poll_result > 0) {
      if ((descriptor.revents & (POLLERR | POLLNVAL)) != 0) {
        throw AppError("network_failed", "Agent socket closed unexpectedly");
      }
      if ((descriptor.revents & events) != 0) {
        return;
      }
      if (events == POLLIN && (descriptor.revents & POLLHUP) != 0) {
        return;
      }
    } else if (poll_result < 0 && errno != EINTR) {
      throw AppError("network_failed", "poll failed: " + std::string(std::strerror(errno)));
    }
  }
}

int connect_http(const Endpoint& endpoint, const Config& config,
                 const std::chrono::steady_clock::time_point& request_deadline) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* addresses = nullptr;
  const int lookup = getaddrinfo(endpoint.host.c_str(), endpoint.port.c_str(), &hints, &addresses);
  if (lookup != 0) {
    throw AppError("network_failed", "cannot resolve Agent host: " + std::string(gai_strerror(lookup)));
  }

  const auto connect_deadline = std::min(
      request_deadline,
      std::chrono::steady_clock::now() + std::chrono::milliseconds(config.connect_timeout_ms));
  int socket_fd = -1;
  for (addrinfo* address = addresses; address != nullptr; address = address->ai_next) {
    socket_fd = socket(address->ai_family, address->ai_socktype, address->ai_protocol);
    if (socket_fd < 0) {
      continue;
    }
    const int flags = fcntl(socket_fd, F_GETFL, 0);
    if (flags < 0 || fcntl(socket_fd, F_SETFL, flags | O_NONBLOCK) < 0) {
      close(socket_fd);
      socket_fd = -1;
      continue;
    }
    const int result = connect(socket_fd, address->ai_addr, address->ai_addrlen);
    if (result == 0) {
      break;
    }
    if (errno == EINPROGRESS) {
      try {
        wait_for_fd(socket_fd, POLLOUT, connect_deadline);
        int socket_error = 0;
        socklen_t socket_error_size = sizeof(socket_error);
        if (getsockopt(socket_fd, SOL_SOCKET, SO_ERROR, &socket_error, &socket_error_size) == 0 &&
            socket_error == 0) {
          break;
        }
      } catch (const AppError&) {
        close(socket_fd);
        freeaddrinfo(addresses);
        throw;
      }
    }
    close(socket_fd);
    socket_fd = -1;
  }
  freeaddrinfo(addresses);
  if (socket_fd < 0) {
    throw AppError("connection_failed", "cannot connect to Agent endpoint");
  }
  return socket_fd;
}

void send_all(int socket_fd, const std::string& data,
              const std::chrono::steady_clock::time_point& deadline) {
  std::size_t offset = 0;
  while (offset < data.size()) {
    wait_for_fd(socket_fd, POLLOUT, deadline);
    const ssize_t sent = send(socket_fd, data.data() + offset, data.size() - offset, MSG_NOSIGNAL);
    if (sent > 0) {
      offset += static_cast<std::size_t>(sent);
    } else if (sent < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
      throw AppError("network_failed", "send failed: " + std::string(std::strerror(errno)));
    }
  }
}

HttpResponse post_json(const Config& config, const std::string& body) {
  const Endpoint endpoint = parse_endpoint(config.agent_base_url);
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(config.request_timeout_ms);
  const int socket_fd = connect_http(endpoint, config, deadline);
  try {
    const std::string path = endpoint.base_path + "/api/chat";
    const std::string request = "POST " + path + " HTTP/1.1\r\nHost: " + endpoint.host +
                                "\r\nContent-Type: application/json\r\nContent-Length: " +
                                std::to_string(body.size()) +
                                "\r\nConnection: close\r\n\r\n" + body;
    send_all(socket_fd, request, deadline);

    std::string response;
    std::array<char, 4096> buffer{};
    while (true) {
      if (g_stop_requested != 0) {
        throw AppError("cancelled", "SIGTERM received during Agent request");
      }
      wait_for_fd(socket_fd, POLLIN, deadline);
      const ssize_t received = recv(socket_fd, buffer.data(), buffer.size(), 0);
      if (received > 0) {
        response.append(buffer.data(), static_cast<std::size_t>(received));
      } else if (received == 0) {
        break;
      } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        throw AppError("network_failed", "recv failed: " + std::string(std::strerror(errno)));
      }
    }
    close(socket_fd);

    const auto header_end = response.find("\r\n\r\n");
    if (header_end == std::string::npos) {
      throw AppError("invalid_http_response", "Agent returned an incomplete HTTP response");
    }
    const auto line_end = response.find("\r\n");
    if (line_end == std::string::npos) {
      throw AppError("invalid_http_response", "Agent returned an invalid HTTP status line");
    }
    std::istringstream status_line(response.substr(0, line_end));
    std::string protocol;
    int status = 0;
    status_line >> protocol >> status;
    if (protocol.rfind("HTTP/", 0) != 0 || status == 0) {
      throw AppError("invalid_http_response", "Agent returned an invalid HTTP status line");
    }
    return {status, response.substr(header_end + 4U)};
  } catch (...) {
    close(socket_fd);
    throw;
  }
}

HttpResponse get_http(const Config& config, const std::string& url) {
  const Endpoint endpoint = parse_endpoint(url);
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(config.request_timeout_ms);
  const int socket_fd = connect_http(endpoint, config, deadline);
  try {
    const std::string path = endpoint.base_path.empty() ? "/" : endpoint.base_path;
    const std::string request = "GET " + path + " HTTP/1.1\r\nHost: " + endpoint.host +
                                "\r\nConnection: close\r\n\r\n";
    send_all(socket_fd, request, deadline);

    std::string response;
    std::array<char, 4096> buffer{};
    while (true) {
      if (g_stop_requested != 0) {
        throw AppError("cancelled", "SIGTERM received during backend health check");
      }
      wait_for_fd(socket_fd, POLLIN, deadline);
      const ssize_t received = recv(socket_fd, buffer.data(), buffer.size(), 0);
      if (received > 0) {
        response.append(buffer.data(), static_cast<std::size_t>(received));
      } else if (received == 0) {
        break;
      } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        throw AppError("network_failed", "recv failed: " + std::string(std::strerror(errno)));
      }
    }
    close(socket_fd);

    const auto header_end = response.find("\r\n\r\n");
    const auto line_end = response.find("\r\n");
    if (header_end == std::string::npos || line_end == std::string::npos) {
      throw AppError("invalid_http_response", "backend returned an incomplete HTTP response");
    }
    std::istringstream status_line(response.substr(0, line_end));
    std::string protocol;
    int status = 0;
    status_line >> protocol >> status;
    if (protocol.rfind("HTTP/", 0) != 0 || status == 0) {
      throw AppError("invalid_http_response", "backend returned an invalid HTTP status line");
    }
    return {status, response.substr(header_end + 4U)};
  } catch (...) {
    close(socket_fd);
    throw;
  }
}

struct CommandResult {
  int exit_code;
  std::string output;
};

CommandResult run_command(const std::vector<std::string>& arguments, int timeout_ms,
                          bool ignore_stop_request = false) {
  if (arguments.empty()) {
    throw AppError("backend_docker_failed", "empty command");
  }
  int output_pipe[2]{};
  if (pipe(output_pipe) != 0) {
    throw AppError("backend_docker_failed", "cannot create Docker command pipe");
  }
  const pid_t child = fork();
  if (child < 0) {
    close(output_pipe[0]);
    close(output_pipe[1]);
    throw AppError("backend_docker_failed", "cannot start Docker command");
  }
  if (child == 0) {
    dup2(output_pipe[1], STDOUT_FILENO);
    dup2(output_pipe[1], STDERR_FILENO);
    close(output_pipe[0]);
    close(output_pipe[1]);
    std::vector<char*> argv;
    argv.reserve(arguments.size() + 1U);
    for (const std::string& argument : arguments) {
      argv.push_back(const_cast<char*>(argument.c_str()));
    }
    argv.push_back(nullptr);
    execvp(argv[0], argv.data());
    _exit(127);
  }

  close(output_pipe[1]);
  const int flags = fcntl(output_pipe[0], F_GETFL, 0);
  if (flags >= 0) {
    fcntl(output_pipe[0], F_SETFL, flags | O_NONBLOCK);
  }
  std::string output;
  std::array<char, 1024> buffer{};
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
  int wait_status = 0;
  bool exited = false;
  while (!exited) {
    while (true) {
      const ssize_t received = read(output_pipe[0], buffer.data(), buffer.size());
      if (received > 0) {
        output.append(buffer.data(), static_cast<std::size_t>(received));
      } else if (received < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
        break;
      } else {
        break;
      }
    }
    const pid_t wait_result = waitpid(child, &wait_status, WNOHANG);
    if (wait_result == child) {
      exited = true;
      break;
    }
    if (wait_result < 0) {
      close(output_pipe[0]);
      throw AppError("backend_docker_failed", "cannot wait for Docker command");
    }
    if ((!ignore_stop_request && g_stop_requested != 0) || remaining_ms(deadline) == 0) {
      kill(child, SIGTERM);
      waitpid(child, &wait_status, 0);
      close(output_pipe[0]);
      throw AppError((!ignore_stop_request && g_stop_requested != 0) ? "cancelled"
                                                                      : "backend_docker_timeout",
                     (!ignore_stop_request && g_stop_requested != 0)
                         ? "SIGTERM received during Docker command"
                                           : "Docker command timed out");
    }
    usleep(10 * 1000);
  }
  while (true) {
    const ssize_t received = read(output_pipe[0], buffer.data(), buffer.size());
    if (received > 0) {
      output.append(buffer.data(), static_cast<std::size_t>(received));
    } else {
      break;
    }
  }
  close(output_pipe[0]);
  const int exit_code = WIFEXITED(wait_status) ? WEXITSTATUS(wait_status) : 128;
  return {exit_code, trim(output)};
}

bool docker_is_running(const Config& config, const std::string& container) {
  const CommandResult result = run_command(
      {config.docker_path, "inspect", "--format", "{{.State.Running}}", container},
      config.connect_timeout_ms);
  if (result.exit_code != 0) {
    throw AppError("backend_docker_failed", "cannot inspect " + container + ": " + result.output);
  }
  if (result.output == "true") {
    return true;
  }
  if (result.output == "false") {
    return false;
  }
  throw AppError("backend_docker_failed", "unexpected Docker state for " + container);
}

void docker_start(const Config& config, const std::string& container) {
  const CommandResult result = run_command({config.docker_path, "start", container},
                                           config.connect_timeout_ms);
  if (result.exit_code != 0) {
    throw AppError("backend_docker_failed", "cannot start " + container + ": " + result.output);
  }
}

void docker_stop(const Config& config, const std::string& container) {
  try {
    const bool cancelling = g_stop_requested != 0;
    const CommandResult result = run_command(
        {config.docker_path, "stop", "--time", cancelling ? "1" : "5", container},
        cancelling ? 2000 : 7000, true);
    if (result.exit_code != 0) {
      std::cerr << "warning: cannot stop " << container << ": " << result.output << '\n';
    }
  } catch (const AppError& error) {
    std::cerr << "warning: cannot stop " << container << ": " << error.what() << '\n';
  }
}

void wait_for_healthy(const Config& config, const std::string& name, const std::string& health_url) {
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(config.backend_start_timeout_ms);
  std::string last_error = name + " is not ready";
  while (remaining_ms(deadline) > 0) {
    if (g_stop_requested != 0) {
      throw AppError("cancelled", "SIGTERM received while waiting for " + name);
    }
    try {
      const HttpResponse response = get_http(config, health_url);
      if (response.status >= 200 && response.status < 300) {
        return;
      }
    } catch (const AppError& error) {
      last_error = error.what();
    }
    usleep(static_cast<useconds_t>(std::min(config.backend_poll_interval_ms, 1000)) * 1000U);
  }
  throw AppError("backend_not_ready", name + " did not become ready: " + last_error);
}

class BackendLifecycle {
 public:
  explicit BackendLifecycle(const Config& config) : config_(config) {}

  void start() {
    start_if_needed(config_.llm_container, config_.llm_health_url, llm_started_);
    start_if_needed(config_.agent_container, config_.agent_health_url, agent_started_);
  }

  ~BackendLifecycle() {
    if (agent_started_) {
      docker_stop(config_, config_.agent_container);
    }
    if (llm_started_) {
      docker_stop(config_, config_.llm_container);
    }
  }

 private:
  void start_if_needed(const std::string& container, const std::string& health_url, bool& started) {
    if (docker_is_running(config_, container)) {
      std::cout << "backend already running: " << container << '\n';
      return;
    }
    std::cout << "starting backend: " << container << '\n';
    docker_start(config_, container);
    started = true;
    wait_for_healthy(config_, container, health_url);
    std::cout << "backend ready: " << container << '\n';
  }

  const Config& config_;
  bool llm_started_ = false;
  bool agent_started_ = false;
};

std::size_t skip_whitespace(const std::string& text, std::size_t position) {
  while (position < text.size() && std::isspace(static_cast<unsigned char>(text[position])) != 0) {
    ++position;
  }
  return position;
}

std::uint32_t parse_hex_codepoint(const std::string& json, std::size_t position) {
  if (position + 4U > json.size()) {
    throw AppError("invalid_agent_response", "Agent answer has an invalid unicode escape");
  }
  std::uint32_t codepoint = 0;
  for (std::size_t offset = 0; offset < 4U; ++offset) {
    const unsigned char ch = static_cast<unsigned char>(json[position + offset]);
    codepoint <<= 4U;
    if (ch >= '0' && ch <= '9') {
      codepoint |= ch - '0';
    } else if (ch >= 'a' && ch <= 'f') {
      codepoint |= ch - 'a' + 10U;
    } else if (ch >= 'A' && ch <= 'F') {
      codepoint |= ch - 'A' + 10U;
    } else {
      throw AppError("invalid_agent_response", "Agent answer has an invalid unicode escape");
    }
  }
  return codepoint;
}

void append_utf8(std::string& output, std::uint32_t codepoint) {
  if (codepoint <= 0x7FU) {
    output.push_back(static_cast<char>(codepoint));
  } else if (codepoint <= 0x7FFU) {
    output.push_back(static_cast<char>(0xC0U | (codepoint >> 6U)));
    output.push_back(static_cast<char>(0x80U | (codepoint & 0x3FU)));
  } else if (codepoint <= 0xFFFFU) {
    output.push_back(static_cast<char>(0xE0U | (codepoint >> 12U)));
    output.push_back(static_cast<char>(0x80U | ((codepoint >> 6U) & 0x3FU)));
    output.push_back(static_cast<char>(0x80U | (codepoint & 0x3FU)));
  } else if (codepoint <= 0x10FFFFU) {
    output.push_back(static_cast<char>(0xF0U | (codepoint >> 18U)));
    output.push_back(static_cast<char>(0x80U | ((codepoint >> 12U) & 0x3FU)));
    output.push_back(static_cast<char>(0x80U | ((codepoint >> 6U) & 0x3FU)));
    output.push_back(static_cast<char>(0x80U | (codepoint & 0x3FU)));
  } else {
    throw AppError("invalid_agent_response", "Agent answer has an invalid unicode code point");
  }
}

std::optional<std::size_t> find_key_value(const std::string& json, const std::string& key) {
  const std::string quoted = "\"" + key + "\"";
  const auto key_position = json.find(quoted);
  if (key_position == std::string::npos) {
    return std::nullopt;
  }
  const auto colon = json.find(':', key_position + quoted.size());
  if (colon == std::string::npos) {
    return std::nullopt;
  }
  return skip_whitespace(json, colon + 1U);
}

std::string parse_json_string(const std::string& json, std::size_t position) {
  if (position >= json.size() || json[position] != '"') {
    throw AppError("invalid_agent_response", "Agent answer is not a JSON string");
  }
  std::string value;
  ++position;
  while (position < json.size()) {
    const char ch = json[position++];
    if (ch == '"') {
      return value;
    }
    if (ch != '\\') {
      value.push_back(ch);
      continue;
    }
    if (position >= json.size()) {
      break;
    }
    const char escaped = json[position++];
    switch (escaped) {
      case '"': value.push_back('"'); break;
      case '\\': value.push_back('\\'); break;
      case '/': value.push_back('/'); break;
      case 'b': value.push_back('\b'); break;
      case 'f': value.push_back('\f'); break;
      case 'n': value.push_back('\n'); break;
      case 'r': value.push_back('\r'); break;
      case 't': value.push_back('\t'); break;
      case 'u': {
        std::uint32_t codepoint = parse_hex_codepoint(json, position);
        position += 4U;
        if (codepoint >= 0xD800U && codepoint <= 0xDBFFU) {
          if (position + 6U > json.size() || json[position] != '\\' ||
              json[position + 1U] != 'u') {
            throw AppError("invalid_agent_response", "Agent answer has an incomplete unicode surrogate");
          }
          const std::uint32_t low_surrogate = parse_hex_codepoint(json, position + 2U);
          if (low_surrogate < 0xDC00U || low_surrogate > 0xDFFFU) {
            throw AppError("invalid_agent_response", "Agent answer has an invalid unicode surrogate");
          }
          position += 6U;
          codepoint = 0x10000U + ((codepoint - 0xD800U) << 10U) +
                      (low_surrogate - 0xDC00U);
        } else if (codepoint >= 0xDC00U && codepoint <= 0xDFFFU) {
          throw AppError("invalid_agent_response", "Agent answer has an invalid unicode surrogate");
        }
        append_utf8(value, codepoint);
        break;
      }
      default: throw AppError("invalid_agent_response", "Agent answer has an invalid JSON escape");
    }
  }
  throw AppError("invalid_agent_response", "Agent answer JSON string is incomplete");
}

std::string extract_json_value(const std::string& json, std::size_t position) {
  if (position >= json.size() || (json[position] != '[' && json[position] != '{')) {
    throw AppError("invalid_agent_response", "Agent tool_calls is not a JSON array");
  }
  const char opening = json[position];
  const char closing = opening == '[' ? ']' : '}';
  std::size_t depth = 0;
  bool in_string = false;
  bool escaped = false;
  for (std::size_t index = position; index < json.size(); ++index) {
    const char ch = json[index];
    if (in_string) {
      if (escaped) {
        escaped = false;
      } else if (ch == '\\') {
        escaped = true;
      } else if (ch == '"') {
        in_string = false;
      }
      continue;
    }
    if (ch == '"') {
      in_string = true;
    } else if (ch == opening) {
      ++depth;
    } else if (ch == closing) {
      --depth;
      if (depth == 0) {
        return json.substr(position, index - position + 1U);
      }
    }
  }
  throw AppError("invalid_agent_response", "Agent tool_calls JSON is incomplete");
}

struct AgentResult {
  std::string answer;
  std::string tool_calls;
};

AgentResult parse_agent_result(const HttpResponse& response, bool require_gpu_tool) {
  if (response.status < 200 || response.status >= 300) {
    throw AppError("agent_http_error", "Agent returned HTTP " + std::to_string(response.status));
  }
  const auto answer_position = find_key_value(response.body, "answer");
  const auto calls_position = find_key_value(response.body, "tool_calls");
  if (!answer_position || !calls_position) {
    throw AppError("invalid_agent_response", "Agent response must contain answer and tool_calls");
  }
  AgentResult result{parse_json_string(response.body, *answer_position),
                     extract_json_value(response.body, *calls_position)};
  if (require_gpu_tool && result.tool_calls.find("\"get_gpu_status\"") == std::string::npos) {
    throw AppError("gpu_tool_not_called", "Agent did not call get_gpu_status");
  }
  return result;
}

std::string timestamp_utc() {
  const auto now = std::chrono::system_clock::now();
  const std::time_t now_time = std::chrono::system_clock::to_time_t(now);
  std::tm utc{};
  gmtime_r(&now_time, &utc);
  std::ostringstream stream;
  stream << std::put_time(&utc, "%Y-%m-%dT%H:%M:%SZ");
  return stream.str();
}

void write_result(const std::filesystem::path& path, const std::string& content) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output) {
    throw AppError("result_open_failed", "cannot overwrite result file: " + path.string());
  }
  output << content;
  output.flush();
  if (!output) {
    throw AppError("result_write_failed", "cannot write result file: " + path.string());
  }
}

std::string make_success_json(const std::string& mode, unsigned int preset_id,
                              const std::optional<std::string>& input, const AgentResult& result) {
  std::ostringstream output;
  output << "{\"mode\":\"" << json_escape(mode) << "\",\"input\":";
  if (input) {
    output << "{\"preset_id\":" << preset_id << ",\"path\":\"" << json_escape(*input)
           << "\"}";
  } else {
    output << "null";
  }
  output << ",\"answer\":\"" << json_escape(result.answer) << "\",\"tool_calls\":"
         << result.tool_calls << ",\"timestamp\":\"" << timestamp_utc() << "\"}";
  return output.str();
}

std::string make_error_json(const std::string& mode, unsigned int preset_id,
                            const std::optional<std::string>& input, const AppError& error) {
  std::ostringstream output;
  output << "{\"mode\":\"" << json_escape(mode) << "\",\"input\":";
  if (input) {
    output << "{\"preset_id\":" << preset_id << ",\"path\":\"" << json_escape(*input)
           << "\"}";
  } else {
    output << "null";
  }
  output << ",\"answer\":\"\",\"tool_calls\":[],\"timestamp\":\"" << timestamp_utc()
         << "\",\"error\":{\"code\":\"" << json_escape(error.code)
         << "\",\"message\":\"" << json_escape(error.what()) << "\"}}";
  return output.str();
}

int run(int argc, char* argv[]) {
  if (argc != 10) {
    std::cerr << "expected argc=10, got " << argc << '\n';
    return 64;
  }
  const std::filesystem::path work_dir = argv[3];
  const std::filesystem::path raw_dir = work_dir.parent_path() / "raw";
  const std::filesystem::path result_path = argv[6];
  std::string mode = "unknown";
  std::optional<std::string> input;
  unsigned int preset_id = 0;
  std::unique_ptr<TelemetryReporter> telemetry;

  try {
    const std::string mode_argument = argv[1];
    if (mode_argument == "1") {
      mode = "image_recognition";
    } else if (mode_argument == "2") {
      mode = "text_prompt";
    } else {
      throw AppError("unsupported_mode", "argv[1] must be 1 (image_recognition) or 2 (text_prompt)");
    }

    preset_id = parse_preset_index(argv[2]);
    const std::uint8_t device_code = parse_device_code(argv[9]);
    const Config config = load_config(work_dir / "lib" / "app_1.conf");
    std::cout << "starting mode=" << mode << " agent=" << config.agent_base_url
              << " preset_id=" << preset_id << " device_code=" << argv[9] << '\n';
    telemetry = std::make_unique<TelemetryReporter>(argv[8], device_code);
    telemetry->start();
    AgentResult result;
    {
      BackendLifecycle backends(config);
      backends.start();
      if (mode == "image_recognition") {
        const std::filesystem::path image_path = select_image_preset(raw_dir, preset_id);
        input = image_path.string();
        const auto [mime, bytes] = read_image(image_path, config.max_image_bytes);
        const std::string data_url = "data:" + mime + ";base64," + base64_encode(bytes);
        const std::string request =
            "{\"message\":\"请识别并简洁描述这张图片中的主要内容、关键对象和异常或重要信息。"
            "仅基于图像回答。\",\"image_url\":\"" + json_escape(data_url) + "\"}";
        result = parse_agent_result(post_json(config, request), false);
      } else {
        std::filesystem::path prompt_path;
        const std::string prompt =
            read_prompt(raw_dir, preset_id, config.max_prompt_bytes, prompt_path);
        input = prompt_path.string();
        const std::string request = "{\"message\":\"" + json_escape(prompt) + "\"}";
        result = parse_agent_result(post_json(config, request), false);
      }
    }
    write_result(result_path, make_success_json(mode, preset_id, input, result));
    telemetry->finish(TelemetryStatus::kSuccess);
    std::cout << "completed mode=" << mode << " result=" << result_path << '\n';
    return 0;
  } catch (const AppError& error) {
    std::cerr << "failed mode=" << mode << " code=" << error.code << " message=" << error.what()
              << '\n';
    try {
      write_result(result_path, make_error_json(mode, preset_id, input, error));
    } catch (const AppError& write_error) {
      std::cerr << "failed to write error result: " << write_error.what() << '\n';
    }
    if (telemetry) {
      telemetry->finish(error.code == "cancelled" ? TelemetryStatus::kCancelled
                                                   : TelemetryStatus::kFailure);
    }
    return error.code == "cancelled" ? 143 : 1;
  } catch (const std::exception& error) {
    std::cerr << "unexpected failure: " << error.what() << '\n';
    if (telemetry) {
      telemetry->finish(TelemetryStatus::kFailure);
    }
    return 1;
  }
}

}  // namespace

int main(int argc, char* argv[]) {
  struct sigaction action {};
  action.sa_handler = on_sigterm;
  sigemptyset(&action.sa_mask);
  action.sa_flags = 0;
  sigaction(SIGTERM, &action, nullptr);
  return run(argc, argv);
}
