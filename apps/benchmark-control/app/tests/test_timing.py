"""Request-window recovery and evidence rejection, including mixed outcomes."""
from copy import deepcopy
import unittest

from app.timing import audit_timing
from app.reporting import groups_from_snapshot, render_detail
from app.tests.test_report_presentation import source
from app.tests.test_reporting import fixture


def benchmark():
    # The failed request extends past the successful one; its time must count.
    return dict(duration=20, cooldown_duration=0,
        scheduler_metrics=dict(start_time=99, end_time=121, measure_start_time=100,
            measure_end_time=120, request_start_time=100, request_end_time=120),
        metrics=dict(request_totals=dict(successful=1, errored=1, incomplete=1, total=3),
            output_token_count={'successful':dict(count=1,total_sum=100)}),
        requests=dict(successful=[dict(request_id='success',request_start_time=100,request_end_time=110,output_tokens=100)],
            errored=[dict(request_id='error',request_start_time=105,request_end_time=120)],
            incomplete=[dict(request_id='cancelled',request_start_time=99.5,request_end_time=119)]))


def valid_benchmark():
    b=benchmark();b['requests']['incomplete'][0]['request_start_time']=100
    return b


class TimingTests(unittest.TestCase):
    def test_denominator_includes_all_outcomes_and_successful_tokens_only(self):
        result=audit_timing(valid_benchmark())
        self.assertEqual(result['full_run_status'],'reconciled')
        self.assertEqual(result['full_run_output_tps'],5)
        self.assertEqual(result['native_timing_status'],'consistent')

    def test_detects_future_window_and_recovers_full_run_without_rewriting_native(self):
        b=valid_benchmark();b.update(duration=1495,cooldown_duration=-1475)
        b['scheduler_metrics']['measure_end_time']=1595
        original=deepcopy(b);result=audit_timing(b)
        self.assertEqual(result['native_timing_status'],'inconsistent')
        self.assertIn('Native measurement ends after scheduler execution',result['native_timing_findings'])
        self.assertEqual(result['full_run_output_tps'],5)
        self.assertEqual(b,original)

    def test_missing_sampled_duplicate_or_bad_records_withhold_rates(self):
        mutations=[
            lambda b:b['requests']['successful'].clear(),
            lambda b:b['requests']['errored'][0].update(request_id='success'),
            lambda b:b['requests']['incomplete'][0].pop('request_end_time'),
            lambda b:b['requests']['successful'][0].update(output_tokens=None),
            lambda b:b['requests']['successful'][0].update(output_tokens=101),
            lambda b:b['requests']['successful'][0].update(output_tokens=True),
            lambda b:b['requests']['successful'][0].update(request_end_time=99),
            lambda b:b['requests']['successful'][0].update(request_end_time=float('inf')),
            lambda b:b['scheduler_metrics'].update(request_end_time=118),
            lambda b:b['scheduler_metrics'].update(end_time=115),
        ]
        for mutate in mutations:
            b=valid_benchmark();mutate(b)
            with self.subTest(mutate=mutate):
                result=audit_timing(b)
                self.assertIsNone(result['full_run_output_tps'])
                self.assertEqual(result['full_run_status'],'unavailable')
                self.assertTrue(result['full_run_findings'])

    def test_no_evidence_does_not_fabricate_rates_or_validation(self):
        result=audit_timing({})
        self.assertEqual(result['native_timing_status'],'not_checked')
        self.assertIsNone(result['full_run_output_tps'])

    def test_zero_success_is_zero_delivered_throughput_not_missing(self):
        b=valid_benchmark();b['requests']['successful']=[]
        b['metrics']['request_totals'].update(successful=0,total=2)
        b['metrics']['output_token_count']['successful'].update(count=0,total_sum=0)
        self.assertEqual(audit_timing(b)['full_run_output_tps'],0)

    def test_report_uses_recovered_rates_and_keeps_native_in_audit_table(self):
        data=fixture();b=valid_benchmark()
        b['config']={'strategy':{'streams':1,'type_':'concurrent'}}
        b['metrics']['output_tokens_per_second']={'successful':{'mean':0.067}}
        b['metrics']['time_to_first_token_ms']={'successful':{'percentiles':{'p95':50}}}
        b.update(duration=1495,cooldown_duration=-1475)
        b['scheduler_metrics']['measure_end_time']=1595
        data['guidellm_json']={'benchmarks':[b]}
        group=groups_from_snapshot({'sources':[source(1,data,status='failed')]})[0]
        point=group['points'][0]
        self.assertEqual(point['native_system_tps'],0.067)
        self.assertEqual(point['reconstructed_system_tps'],5)
        self.assertEqual(point['full_run_tps_per_concurrency'],5)
        page=render_detail(group,'Timing regression')
        self.assertIn('Full-run system TPS vs. concurrency',page)
        self.assertIn('GuideLLM native output tokens/s (audit)',page)
        self.assertIn('Native measurement ends after scheduler execution',page)
        self.assertEqual(page.count('<circle'),4)

    def test_complete_reconciled_evidence_has_no_blanket_timing_warning(self):
        data=fixture('none');b=data['guidellm_json']['benchmarks'][0]
        b.update(cooldown_duration=0,
            scheduler_metrics=dict(start_time=99,end_time=111,measure_start_time=100,
                measure_end_time=110,request_start_time=100,request_end_time=110),
            requests=dict(successful=[dict(request_id=str(i),request_start_time=100+i,
                request_end_time=101+i,output_tokens=128) for i in range(10)],errored=[],incomplete=[]))
        b['metrics']['output_token_count']={'successful':dict(count=10,total_sum=1280)}
        b['metrics']['output_tokens_per_second']['successful']['mean']=128
        data['guidellm_json']={'benchmarks':[b]}
        point=groups_from_snapshot({'sources':[source(1,data)]})[0]['points'][0]
        self.assertEqual(point['reconstructed_system_tps'],128)
        self.assertEqual(point['evidence_status'],'checked')
        self.assertEqual(point['warnings'],[])


if __name__ == '__main__':unittest.main()
