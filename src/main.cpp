#include <string>
#include <iostream>
#include <CLI/CLI.hpp>

int main(int argc, char **argv) {
    auto app = CLI::App {"huitacam"};
    std::string config_dir;
    app.add_option("-c,--config-dir", config_dir, "Config directory")
        ->check(CLI::ExistingDirectory)
        ->required();
    CLI11_PARSE(app, argc, argv);
}
