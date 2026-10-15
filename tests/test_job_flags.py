from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestration"))

import job_poller
from crm_payload import OFFSCOPE_SKILLS, to_crm_job, to_crm_payload
from job_schema import to_report_record


def record(
    *, budget=250, country="United States", skills=None, interviews=0,
    duration="1 to 3 months", query_name="Keyword: Odoo", job_type="FIXED",
    title="Software development", job_id="example-job", hire_rate=None, open_jobs=None,
):
    result = to_report_record({
        "search.id": job_id,
        "search.title": title,
        "search.job_type": job_type,
        "search.budget": budget,
        "search.client.country": country,
        "search.skills": skills,
        "search.duration": duration,
        "get.data.marketplaceJobPosting.activityStat.jobActivity.totalInvitedToInterview": interviews,
        "get.client_record.hire_rate_percent": hire_rate,
        "get.client_record.open_jobs": open_jobs,
    })
    result["query_name"] = query_name
    return result


class JobFlagTests(unittest.TestCase):
    def reasons(self, **values):
        payload = to_crm_job(record(**values))
        self.assertEqual(payload["flagged"], not bool(payload["flag_reasons"]))
        self.assertEqual(list(payload)[-2:], ["flagged", "flag_reasons"])
        return payload["flag_reasons"]

    def test_each_implemented_rule(self):
        cases = {
            "R1": {"budget": 50, "country": "India"},
            "R2": {"budget": 50, "country": None},
            "R3": {"budget": 50, "skills": ["VIDEO EDITING"]},
            "R9": {"budget": 50, "interviews": 1},
            "R13": {"country": "India", "duration": "LESS THAN 1 MONTH"},
            "R15": {"country": "India", "query_name": "Keyword: PowerBI"},
            "R16": {"country": "India", "interviews": 1},
            "R20": {"country": "India", "query_name": "Keyword: Power BI"},
        }
        for rule, values in cases.items():
            with self.subTest(rule=rule):
                self.assertEqual(self.reasons(**values), [rule])

    def test_both_conditions_are_required(self):
        cases = {
            "R1": ({"budget": 50}, {"country": "India"}),
            "R2": ({"budget": 50}, {"country": None}),
            "R3": ({"budget": 50}, {"skills": ["video editing"]}),
            "R9": ({"budget": 50}, {"interviews": 1}),
            "R13": ({"country": "India"}, {"duration": "Less than 1 month"}),
            "R15": ({"country": "India"}, {"query_name": "Keyword: PowerBI"}),
            "R16": ({"country": "India"}, {"interviews": 1}),
            "R20": ({"country": "India"}, {"query_name": "Keyword: Power BI"}),
        }
        for rule, sides in cases.items():
            for values in sides:
                with self.subTest(rule=rule, values=values):
                    self.assertNotIn(rule, self.reasons(**values))

    def test_fixed_price_boundaries_and_hourly_jobs(self):
        for budget in (None, 0, -1, 100, 100.01):
            with self.subTest(budget=budget):
                self.assertEqual(self.reasons(budget=budget, country="India"), [])
        for budget in (0.01, 99.99):
            with self.subTest(budget=budget):
                self.assertEqual(self.reasons(budget=budget, country="India"), ["R1"])
        self.assertEqual(self.reasons(budget=50, country="India", job_type="hourly"), [])

    def test_countries_match_case_insensitively_by_name_or_code(self):
        countries = (
            ("India", "IND", "IN"),
            ("Pakistan", "PAK", "PK"),
            ("Nigeria", "NGA", "NG"),
            ("Israel", "ISR", "IL"),
            ("Bangladesh", "BGD", "BD"),
            ("Philippines", "PHL", "PH"),
        )
        for variants in countries:
            for country in variants:
                with self.subTest(country=country):
                    self.assertEqual(self.reasons(budget=50, country=country), ["R1"])
                    self.assertEqual(self.reasons(budget=50, country=f"  {country.swapcase()}  "), ["R1"])
        for country in ("United States", "USA", "India elsewhere"):
            with self.subTest(country=country):
                self.assertEqual(self.reasons(budget=50, country=country), [])

    def test_missing_country_is_only_null_or_empty(self):
        for country in (None, ""):
            self.assertEqual(self.reasons(budget=50, country=country), ["R2"])
        self.assertEqual(self.reasons(budget=50, country=" "), [])

    def test_all_offscope_skills_match_case_insensitive_substrings(self):
        for skill in OFFSCOPE_SKILLS:
            with self.subTest(skill=skill):
                self.assertEqual(self.reasons(budget=50, skills=["Prefix " + skill.upper() + " suffix"]), ["R3"])
        self.assertEqual(self.reasons(budget=50, skills=["Python", "PostgreSQL"]), [])

    def test_interviewing_requires_a_positive_number(self):
        for count in (None, 0, -1, True, "1"):
            with self.subTest(count=count):
                self.assertEqual(self.reasons(budget=50, interviews=count), [])
        for count in (1, 0.5):
            self.assertEqual(self.reasons(budget=50, interviews=count), ["R9"])

    def test_query_names_match_exactly(self):
        self.assertEqual(self.reasons(country="India", query_name="keyword: PowerBI"), [])
        self.assertEqual(self.reasons(country="India", query_name="Keyword: PowerBI "), [])

    def test_multiple_matches_keep_rule_order(self):
        self.assertEqual(
            self.reasons(budget=50, country="India", duration="Less than 1 month"),
            ["R1", "R13"],
        )
        self.assertEqual(
            self.reasons(budget=50, country="India", skills=["Google Ads"], interviews=2,
                         duration="Less than 1 month", query_name="Keyword: Power BI"),
            ["R1", "R3", "R9", "R13", "R16", "R20"],
        )

    def test_null_data_and_titles_do_not_add_rules(self):
        self.assertEqual(self.reasons(budget=None, country=None, skills=None, interviews=None,
                                     duration=None, query_name=None), [])
        job = record(title="VIDEO EDITING, Android, cold calling")
        job["required_fields"]["client_information"].update(client_hire_rate=0, client_jobs_open=0)
        self.assertEqual(to_crm_job(job)["flag_reasons"], [])

    def test_each_title_rule_matches_and_rejects_unrelated_title(self):
        cases = {
            "R4": (50, "United States", "Senior DevOps Engineer"),
            "R5": (50, "United States", "Salesforce administrator"),
            "R6": (50, "United States", "Data entry clerk"),
            "R7": (50, "United States", "React developer"),
            "R8": (50, "United States", "Cold calling sales rep"),
            "R10": (50, "United States", "Video editor needed"),
            "R11": (50, "United States", "Marketing strategist"),
            "R17": (500, "India", "DevOps engineer"),
            "R18": (500, "India", "Marketing strategist"),
            "R19": (500, "India", "Video editor needed"),
            "R21": (500, "India", "Data entry clerk"),
        }
        for rule, (budget, country, matching_title) in cases.items():
            with self.subTest(rule=rule, title=matching_title):
                self.assertIn(rule, self.reasons(budget=budget, country=country, title=matching_title))
                self.assertNotIn(rule, self.reasons(budget=budget, country=country,
                                                    title="Plain backend engineer"))

    def test_title_rules_require_price_or_location_as_specified(self):
        self.assertEqual(self.reasons(budget=500, country="United States",
                                      title="Senior Salesforce data entry React cold video marketing"), [])
        self.assertEqual(self.reasons(budget=500, country="United States",
                                      title="DevOps video marketing data entry"), [])
        self.assertEqual(self.reasons(budget=50, country="United States",
                                      title="DevOps engineer"), [])

    def test_title_patterns_respect_word_boundaries_and_case(self):
        self.assertEqual(self.reasons(budget=50, title="GHL and NEXT.JS"), ["R5", "R7"])
        self.assertEqual(self.reasons(budget=50, title="leadership AWSome iOSx salesforce"), ["R5"])
        self.assertEqual(self.reasons(budget=500, country="India", title="AWS cloud"), ["R17"])

    def test_requested_title_examples(self):
        self.assertEqual(self.reasons(budget=50, country="United States",
                                      title="Senior DevOps Engineer"), ["R4"])
        self.assertEqual(self.reasons(budget=500, country="India",
                                      title="Video editor needed"), ["R19"])
        self.assertEqual(self.reasons(budget=50, country="India",
                                      title="Cold calling sales rep"), ["R1", "R8"])
        self.assertEqual(self.reasons(budget=50, country="India", skills=["Cold Calling"],
                                      title="Cold calling sales rep"), ["R1", "R3", "R8"])

    def test_r12_requires_target_location_and_at_least_three_open_jobs(self):
        payload = to_crm_job(record(country="India", open_jobs=4))
        self.assertEqual(payload["client_jobs_open"], 4)
        self.assertEqual(payload["flag_reasons"], ["R12"])
        self.assertFalse(payload["flagged"])
        self.assertEqual(self.reasons(country="India", open_jobs=3), ["R12"])
        for values in ({"country": "India", "open_jobs": 2},
                       {"country": "India", "open_jobs": None},
                       {"country": "United States", "open_jobs": 4}):
            with self.subTest(values=values):
                self.assertNotIn("R12", self.reasons(**values))

    def test_r14_uses_percentage_scale_and_requires_target_location(self):
        payload = to_crm_job(record(country="India", hire_rate=15))
        self.assertEqual(payload["client_hire_rate"], 15.0)
        self.assertEqual(payload["flag_reasons"], ["R14"])
        self.assertFalse(payload["flagged"])
        for values in ({"country": "India", "hire_rate": None},
                       {"country": "India", "hire_rate": 0},
                       {"country": "India", "hire_rate": 30},
                       {"country": "India", "hire_rate": 50},
                       {"country": "United States", "hire_rate": 15}):
            with self.subTest(values=values):
                self.assertNotIn("R14", self.reasons(**values))

    def test_clean_and_flagged_jobs_are_all_retained(self):
        records = [record(job_id="clean"), record(job_id="r1", budget=50, country="India"),
                   record(job_id="two", budget=50, country="India", duration="Less than 1 month")]
        original = copy.deepcopy(records)
        payloads = to_crm_payload(records)
        self.assertEqual([p["job_id"] for p in payloads], ["clean", "r1", "two"])
        self.assertEqual([p["flagged"] for p in payloads], [True, False, False])
        self.assertEqual([p["flag_reasons"] for p in payloads], [[], ["R1"], ["R1", "R13"]])
        self.assertEqual(records, original)


class PollerFlagSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_counts_all_jobs_once_and_does_not_filter_sending(self):
        records = [record(job_id="clean"), record(job_id="r1", budget=50, country="India"),
                   record(job_id="two", budget=50, country="India", duration="Less than 1 month"),
                   record(job_id="open", country="India", open_jobs=4),
                   record(job_id="hire", country="India", hire_rate=15)]
        jobs = [{"id": r["required_fields"]["job_information"]["job_id"],
                 "created_date": "2026-10-13T12:00:00Z"} for r in records]
        config = {"org_uid": "test-org", "domains": [{"name": "Keyword: Odoo", "filters": {"query": "Odoo"}}]}

        async def simulated_retries(config, sent_records):
            for _ in range(3):
                self.assertEqual(len(to_crm_payload(sent_records)), 5)
            return True

        with patch.object(job_poller, "fetch_latest_jobs", AsyncMock(return_value=jobs)), \
             patch.object(job_poller, "build_jobs_dataframe", AsyncMock(return_value=object())), \
             patch.object(job_poller, "records_from_dataframe", return_value=records), \
             patch.object(job_poller, "log_jobs_preview"), \
             patch.object(job_poller, "crm_enabled", return_value=True), \
             patch.object(job_poller, "send_jobs", AsyncMock(side_effect=simulated_retries)) as send, \
             patch.object(job_poller, "save_cursor"), \
             patch.object(job_poller.logger, "info") as log:
            await job_poller.poll_once(None, config)

        send.assert_awaited_once()
        self.assertEqual(len(send.await_args.args[1]), 5)
        summaries = [call for call in log.call_args_list if call.args[0].startswith("Job flag summary:")]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].args[1:3], (5, 1))
        expected = {f"R{i}": 0 for i in range(1, 22)}
        expected.update(R1=2, R12=1, R13=1, R14=1)
        self.assertEqual(json.loads(summaries[0].args[3]), expected)

    async def test_empty_run_prints_zero_counts_for_all_twenty_one_rules(self):
        with patch.object(job_poller.logger, "info") as log:
            await job_poller.poll_once(None, {"org_uid": "test-org", "domains": []})
        summary = log.call_args.args
        self.assertEqual(summary[1:3], (0, 0))
        self.assertEqual(json.loads(summary[3]), {f"R{i}": 0 for i in range(1, 22)})


if __name__ == "__main__":
    unittest.main()
