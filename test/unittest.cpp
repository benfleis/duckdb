#include <memory>
#define CATCH_CONFIG_RUNNER
#include "catch.hpp"

#include <mustache.hpp>
#include <yaml-cpp/yaml.h>

#include "duckdb/common/file_system.hpp"
#include "duckdb/common/helper.hpp"
#include "duckdb/common/string_util.hpp"
#include "sqlite/sqllogic_test_logger.hpp"
#include "test_helpers.hpp"
#include "test_config.hpp"

using namespace duckdb;

using kainjow::mustache::data;

namespace duckdb {
shared_ptr<data> ctx = make_shared_ptr<data>();
}

static shared_ptr<data> parse_yaml(const string &yaml_src) {
	YAML::Node config = YAML::LoadFile(yaml_src);
	assert(config.IsMap());
	for (const auto &kv : config) {
		auto key = kv.first.as<std::string>();    // string
		auto value = kv.second.as<std::string>(); // unknown type
		ctx->set(key, value);
	}
	return ctx;
}

int main(int argc_in, char *argv[]) {
	duckdb::unique_ptr<FileSystem> fs = FileSystem::CreateLocal();
	string test_directory = DUCKDB_ROOT_DIRECTORY;

	auto &test_config = TestConfiguration::Get();
	test_config.Initialize();

	idx_t argc = NumericCast<idx_t>(argc_in);
	int new_argc = 0;
	auto new_argv = duckdb::unique_ptr<char *[]>(new char *[argc]);
	for (idx_t i = 0; i < argc; i++) {
		string argument(argv[i]);
		if (argument == "--test-dir") {
			test_directory = string(argv[++i]);
		} else if (argument == "--test-temp-dir") {
			SetDeleteTestPath(false);
			auto test_dir = string(argv[++i]);
			if (fs->DirectoryExists(test_dir)) {
				fprintf(stderr, "--test-temp-dir cannot point to a directory that already exists (%s)\n",
				        test_dir.c_str());
				return 1;
			}
			SetTestDirectory(test_dir);
		} else if (argument == "--require") {
			AddRequire(string(argv[++i]));
		} else if (argument == "--config") {
			auto yaml_src = string(argv[++i]);
			parse_yaml(yaml_src);
		} else if (!test_config.ParseArgument(argument, argc, argv, i)) {
			new_argv[new_argc] = argv[i];
			new_argc++;
		}
	}
	test_config.ChangeWorkingDirectory(test_directory);

	// delete the testing directory if it exists
	auto dir = TestCreatePath("");
	try {
		TestDeleteDirectory(dir);
		// create the empty testing directory
		TestCreateDirectory(dir);
	} catch (std::exception &ex) {
		fprintf(stderr, "Failed to create testing directory \"%s\": %s\n", dir.c_str(), ex.what());
		return 1;
	}

	if (test_config.GetSkipCompiledTests()) {
		Catch::getMutableRegistryHub().clearTests();
	}
	RegisterSqllogictests();
	int result = Catch::Session().run(new_argc, new_argv.get());

	std::string failures_summary = FailureSummary::GetFailureSummary();
	if (!failures_summary.empty()) {
		auto description = test_config.GetDescription();
		if (!description.empty()) {
			std::cerr << "\n====================================================" << std::endl;
			std::cerr << "====================  TEST INFO  ===================" << std::endl;
			std::cerr << "====================================================\n" << std::endl;
			std::cerr << description << std::endl;
		}
		std::cerr << "\n====================================================" << std::endl;
		std::cerr << "================  FAILURES SUMMARY  ================" << std::endl;
		std::cerr << "====================================================\n" << std::endl;
		std::cerr << failures_summary;
	}

	if (DeleteTestPath()) {
		TestDeleteDirectory(dir);
	}

	return result;
}
