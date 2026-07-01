#define CATCH_CONFIG_RUNNER
#include "catch.hpp"

#include "duckdb/common/file_system.hpp"
#include "duckdb/common/string_util.hpp"
#include "sqlite/sqllogic_test_logger.hpp"
#include "test_helpers.hpp"
#include "test_config.hpp"

using namespace duckdb;

int main(int argc_in, char *argv[]) {
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
		} else if (argument == "--temp-dir-base") {
			SetTempDirBase(string(argv[++i]));
		} else if (argument == "--run-id") {
			SetRunId(string(argv[++i]));
		} else if (argument == "--temp-dir-run-id") {
			if (!SetTempDirRunIdInPath(string(argv[++i]))) {
				fprintf(stderr, "--temp-dir-run-id expects one of: on, off\n");
				return 1;
			}
		} else if (argument == "--temp-dir-test-id") {
			if (!SetTempDirTestId(string(argv[++i]))) {
				fprintf(stderr, "--temp-dir-test-id expects one of: on, off\n");
				return 1;
			}
		} else if (argument == "--temp-dir-create") {
			if (!SetTempDirCreate(string(argv[++i]))) {
				fprintf(stderr, "--temp-dir-create expects one of: never, on-absent, always\n");
				return 1;
			}
		} else if (argument == "--temp-dir-destroy") {
			if (!SetTempDirDestroy(string(argv[++i]))) {
				fprintf(stderr, "--temp-dir-destroy expects one of: never, on-success, always\n");
				return 1;
			}
		} else if (argument == "--require") {
			AddRequire(string(argv[++i]));
		} else if (argument == "--emit-on-skip") {
			SetEmitOnSkip(true);
		} else if (!test_config.ParseArgument(argument, argc, argv, i)) {
			new_argv[new_argc] = argv[i];
			new_argc++;
		}
	}
	test_config.ChangeWorkingDirectory(test_directory);

	// Resolve + provision $BASE/[RUN_ID] per the create disposition (the TEST_ID level is
	// materialized later, on the per-test path, once a test name is known).
	string prep_error;
	if (!PrepareTempDir(prep_error)) {
		fprintf(stderr, "Failed to prepare temp directory: %s\n", prep_error.c_str());
		return 1;
	}
	// Capture env now that all --temp-dir-* context (base/run-id/create) is final; must run
	// after PrepareTempDir so TEMP_DIR reflects the materialized run root.
	test_config.UpdateEnvironment();

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

	// Execute the run-id-level destroy disposition ($BASE/[RUN_ID]); pass/fail-aware, recursive.
	DestroyTempDir(result == 0);

	return result;
}
