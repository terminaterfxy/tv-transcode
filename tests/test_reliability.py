"""Failure-path regressions; all media and deletion checks use temporary files."""
import io
import os
import random
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import test_media_pipeline as fixtures

app = fixtures.app


class ReliabilityTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown

    def request(self, data):
        return patch.object(app, 'request', types.SimpleNamespace(get_json=lambda **kw: data))

    def finished_source(self, directory=False):
        source = self.inbox / ('movie' if directory else 'movie.mkv')
        if directory:
            source.mkdir()
            (source / 'movie.mkv').write_bytes(b'original')
        else:
            source.write_bytes(b'original')
        self.movies.mkdir()
        output = self.movies / 'movie.mkv'
        output.write_bytes(b'finished')
        app.ITEMS[source.name] = {'name': source.name, 'status': 'done',
                                 'kind': 'dir' if directory else 'file'}
        app.PROCESSED[source.name] = {'output': str(output),
                                     'source_snapshot': app.source_snapshot(source)}
        return source, output

    def test_multi_video_directory_is_blocked_before_processing(self):
        folder = self.inbox / 'season'
        folder.mkdir()
        (folder / 'ep1.mkv').write_bytes(b'one')
        (folder / 'ep2.mkv').write_bytes(b'two')
        with patch.object(app, 'ffprobe') as ffprobe:
            app.scan_once(); app.scan_once()
        self.assertEqual(app.ITEMS['season']['status'], 'blocked')
        ffprobe.assert_not_called()
        with self.request({'names': ['season']}):
            self.assertEqual(app.api_process()['queued'], [])

    def test_historical_multi_video_directory_cannot_be_deleted(self):
        source, output = self.finished_source(directory=True)
        other = source / 'ep2.mkv'
        other.write_bytes(b'not processed')
        with self.request({'name': source.name}):
            response, status = app.api_delete_source()
        self.assertEqual(status, 409)
        self.assertTrue(other.exists())
        self.assertTrue(output.exists())

    def test_changed_source_cannot_be_deleted(self):
        source, output = self.finished_source()
        source.write_bytes(b'new download')
        with self.request({'name': source.name}):
            response, status = app.api_delete_source()
        self.assertEqual(status, 409)
        self.assertTrue(source.exists())
        self.assertTrue(output.exists())

    def test_old_record_without_snapshot_cannot_delete_source(self):
        source, _ = self.finished_source()
        app.PROCESSED[source.name].pop('source_snapshot')
        with self.request({'name': source.name}):
            _, status = app.api_delete_source()
        self.assertEqual(status, 409)
        self.assertTrue(source.exists())

    def test_unchanged_verified_source_can_be_deleted(self):
        source, output = self.finished_source()
        with self.request({'name': source.name}):
            self.assertTrue(app.api_delete_source()['ok'])
        self.assertFalse(source.exists())
        self.assertTrue(output.exists())

    def test_change_during_copy_does_not_publish_a_finished_movie(self):
        source = self.inbox / 'movie.mkv'
        source.write_bytes(b'original')
        def copying(job, src, target):
            target.write_bytes(b'copy')
            src.write_bytes(b'changed source')
        with patch.object(app, 'ffprobe', return_value={}), \
                patch.object(app, 'summarize_probe', return_value=fixtures.probe()), \
                patch.object(app, 'copy_media_file', side_effect=copying):
            with self.assertRaisesRegex(RuntimeError, '源文件发生变化'):
                app.process_item({'log': []}, {'name': source.name, 'kind': 'file', 'payload': str(source)})
        self.assertEqual(list(self.movies.glob('*')), [])
        self.assertTrue(source.exists())

    def fake_ripper(self, exit_code=0, duration=6000, running=False, stoppable=True,
                    log='MSG:5004,128,2,"已保存 1 个标题，失败 0 个","%1 %2","1","0"\n'):
        observed = {'running': running, 'stops': 0}
        def create(container, cmd, environment=None):
            observed['environment'] = environment
            observed['command'] = cmd
            return {'Id': 'exec-test'}
        def start(exec_id, detach):
            outdir = Path(observed['command'][-1])
            (outdir / 'title.mkv').write_bytes(b'media')
            (outdir / '.makemkv.log').write_text(log)
        def inspect(exec_id):
            return {'Running': observed['running'], 'ExitCode': exit_code}
        def stop(command):
            observed['stops'] += 1
            if stoppable:
                observed['running'] = False
            return types.SimpleNamespace(exit_code=0, output=b'')
        container = types.SimpleNamespace(id='makemkv-id', exec_run=stop)
        client = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda name: container),
            api=types.SimpleNamespace(exec_create=create, exec_start=start, exec_inspect=inspect),
            close=lambda: None,
        )
        media = fixtures.probe(); media['duration'] = duration
        expected = fixtures.probe(); expected['disc_preview'] = {'title': 3}
        return observed, client, media, expected

    def run_fake_rip(self, **kwargs):
        observed, client, media, expected = self.fake_ripper(**kwargs)
        source = self.inbox / 'film.iso'; source.write_bytes(b'disc')
        docker = types.SimpleNamespace(from_env=lambda: client)
        patches = (
            patch.dict(sys.modules, {'docker': docker}),
            patch.object(app, 'makemkv_info', return_value='robot'),
            patch.object(app, 'parse_makemkv_info', return_value=expected),
            patch.object(app, 'summarize_probe', return_value=media),
            patch.object(app, 'ffprobe', return_value={}),
            patch.object(app, 'disk_free', return_value=0 if kwargs.get('running') else 10**15),
            patch.object(app.time, 'sleep'),
        )
        from contextlib import ExitStack
        job = {'log': []}
        with ExitStack() as stack:
            for p in patches: stack.enter_context(p)
            try:
                result = app.makemkv_rip(job, source, self.work / 'rip')
            except RuntimeError as error:
                return observed, job, error
        return observed, job, result

    def test_makemkv_nonzero_exit_rejects_playable_partial_file(self):
        observed, job, result = self.run_fake_rip(exit_code=1)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('退出码 1', str(result))

    def test_makemkv_zero_exit_with_failure_message_is_rejected(self):
        _, _, result = self.run_fake_rip(log='MSG:5003,0,2,"保存失败","%1 %2","3","title.mkv"\n')
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('拒绝使用残片', str(result))

    def test_makemkv_shortened_title_is_rejected(self):
        _, _, result = self.run_fake_rip(duration=120)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('预读不符', str(result))

    def test_makemkv_success_extracts_only_selected_title(self):
        observed, _, result = self.run_fake_rip()
        self.assertIsInstance(result, Path)
        self.assertEqual(observed['command'][-2], '3')
        self.assertEqual(observed['command'][:2], ['/bin/sh', '-c'])
        self.assertIn('wait', observed['command'][2])
        self.assertTrue(result.exists())

    def test_shell_supervisor_waits_for_busybox_style_setsid_exit_status(self):
        observed, _, result = self.run_fake_rip()
        self.assertIsInstance(result, Path)
        bindir = self.work / 'bin'; bindir.mkdir()
        executable = bindir / 'setsid'
        executable.write_text('#!' + sys.executable + '\n' +
            'import os, sys\n'
            'try: os.setsid()\n'
            'except PermissionError:\n'
            '    if os.fork(): sys.exit(0)\n'
            '    os.setsid()\n'
            'os.execvp(sys.argv[1], sys.argv[1:])\n')
        executable.chmod(0o755)
        command = observed['command'][:4] + [sys.executable, '-c',
                    'import time, sys; time.sleep(0.05); sys.exit(7)']
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ.get('PATH', ''))
        completed = subprocess.run(command, env=env, timeout=5, capture_output=True)
        self.assertEqual(completed.returncode, 7, completed.stderr)

    def test_low_disk_stops_remote_extraction(self):
        observed, job, result = self.run_fake_rip(running=True)
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(observed['running'])
        self.assertEqual(observed['stops'], 1)
        self.assertFalse(job.get('preserve_work'))

    def test_unconfirmed_stop_preserves_work_and_blocks_queue(self):
        observed, job, result = self.run_fake_rip(running=True, stoppable=False)
        self.assertTrue(job['preserve_work'])
        self.assertTrue(app.pipeline_block_reason())
        self.assertTrue((self.work / 'rip' / 'title.mkv').exists())
        app.JOB_QUEUE.append('next.mkv')
        self.assertIsNone(app.claim_next_job())
        self.assertEqual(app.JOB_QUEUE, ['next.mkv'])

    def test_large_stderr_does_not_deadlock_progress_reader(self):
        real_popen = subprocess.Popen
        timers = []
        def watched(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            timer = threading.Timer(5, lambda: process.kill() if process.poll() is None else None)
            timer.start(); timers.append(timer)
            return process
        try:
            with patch.object(app.subprocess, 'Popen', side_effect=watched), \
                    patch.object(app, 'disk_free', return_value=10**15):
                job = {'log': []}
                app.run_ffmpeg_progress(job, [sys.executable, '-c',
                    'import sys; sys.stderr.write("E" * 1048576); sys.stderr.flush(); print("out_time_us=1000000")'], 1, 'test')
                self.assertEqual(job['percent'], 100)
        finally:
            for timer in timers: timer.cancel()

    def test_low_disk_stops_even_a_silent_local_process(self):
        created = []
        real_popen = subprocess.Popen
        def watched(*args, **kwargs):
            process = real_popen(*args, **kwargs); created.append(process); return process
        with patch.object(app.subprocess, 'Popen', side_effect=watched), \
                patch.object(app, 'disk_free', return_value=0):
            with self.assertRaisesRegex(RuntimeError, '磁盘空间告急'):
                app.run_ffmpeg_progress({'log': []}, [sys.executable, '-c', 'import time; time.sleep(60)'], 1, 'test')
        self.assertIsNotNone(created[0].poll())

    def test_upgrade_rejects_new_jobs_and_does_not_consume_queue(self):
        app.ITEMS['movie.mkv'] = {'name': 'movie.mkv', 'kind': 'file', 'status': 'ready'}
        for status in app.UPGRADE_BUSY:
            with self.subTest(status=status), patch.object(app, 'read_upgrade_status', return_value={'status': status}):
                with self.request({'names': ['movie.mkv']}):
                    _, code = app.api_process()
                self.assertEqual(code, 409)
                self.assertFalse(app.JOB_QUEUE)
                self.assertIsNone(app.claim_next_job())
        self.assertFalse(app.ACTIVE_OPERATIONS)

    def test_claim_reserves_current_job_atomically(self):
        app.ITEMS['movie.mkv'] = {'name': 'movie.mkv', 'status': 'queued'}
        app.JOB_QUEUE.append('movie.mkv')
        claimed = app.claim_next_job()
        self.assertEqual(claimed['status'], 'processing')
        self.assertEqual(app.CURRENT_JOB['name'], 'movie.mkv')
        self.assertEqual(app.JOB_QUEUE, [])
        self.assertIsNone(app.claim_next_job())

    def test_upgrade_waits_for_subtitle_operation_and_disc_preview(self):
        for field, value in [('ACTIVE_OPERATIONS', {'subtitle'}), ('DISC_PREVIEW_ACTIVE', 'film.iso')]:
            files = {'app_py': io.BytesIO(b'print("ok")'), 'dockerfile': io.BytesIO(b'FROM python:3.11\nCOPY app.py /app/app.py\nCMD ["python","app.py"]')}
            with patch.object(app, field, value), patch.object(app, 'request', types.SimpleNamespace(remote_addr='192.168.1.2', files=files)):
                _, status = app.api_upgrade()
            self.assertEqual(status, 409)

    def test_unrelated_dense_subtitle_timings_are_not_auto_accepted(self):
        randomizer = random.Random(42)
        def cues():
            return [{'start': i * 2000 + randomizer.randrange(1800),
                     'end': i * 2000 + 2000, 'text': 'line'} for i in range(240)]
        self.assertIsNone(app.estimate_subtitle_transform(cues(), cues()))

    def test_negative_shift_discards_cues_before_movie_instead_of_piling_at_zero(self):
        cues = [{'start': 100, 'end': 200, 'text': 'gone'},
                {'start': 500, 'end': 800, 'text': 'keep'}]
        result = app.transform_cues(cues, {'scale': 1, 'offset_ms': -600})
        self.assertEqual(result, [{'start': 0, 'end': 200, 'text': 'keep', 'origin': 'human'}])

    def test_profile_8_is_not_enough_to_claim_profile_81(self):
        media = fixtures.probe('hevc', dv=8)
        media['video'].update(dv_compatibility_id=4, dv_rpu_present=1, dv_bl_present=1, dv_el_present=0)
        with patch.object(app, 'ffprobe', return_value={}), patch.object(app, 'summarize_probe', return_value=media):
            with self.assertRaisesRegex(RuntimeError, 'P8.1'):
                app.verify_dovi_p8(Path('unused'), 6000)
            media['video']['dv_compatibility_id'] = 1
            app.verify_dovi_p8(Path('unused'), 6000)

    def test_p7_rejects_timing_that_cannot_be_rebuilt_as_cfr(self):
        app.validate_p7_timing({'r_frame_rate': '24000/1001', 'avg_frame_rate': '24000/1001'})
        for video in ({'r_frame_rate': '0/0'},
                      {'r_frame_rate': '24/1', 'avg_frame_rate': '20/1'},
                      {'r_frame_rate': '24/1', 'start_time': 60}):
            with self.subTest(video=video), self.assertRaisesRegex(RuntimeError, '音画同步'):
                app.validate_p7_timing(video)

    def test_genuine_global_subtitle_offset_and_rate_are_retained(self):
        randomizer = random.Random(31)
        starts = []
        start = 1000
        for _ in range(90):
            start += randomizer.randrange(9000, 25000)
            starts.append(start)
        candidate = [{'start': n, 'end': n + 1000, 'text': '中'} for n in starts]
        anchor = [{'start': n * 1.001 + 600000, 'end': n * 1.001 + 601000, 'text': '日'} for n in starts]
        transform = app.estimate_subtitle_transform(anchor, candidate)
        self.assertIsNotNone(transform)
        self.assertAlmostEqual(transform['scale'], 1.001)
        self.assertAlmostEqual(transform['offset_ms'], 600000)

    # ---- 转 EAC3 时保留原始音轨（MKV 成品） ----
    # ---- 可不处理的文件：原样入库 ----
    def optional_probe(self, **kw):
        subs = kw.pop('subs', [{'codec': 'hdmv_pgs_subtitle', 'lang': 'chi', 'title': ''},
                               {'codec': 'subrip', 'lang': 'chi', 'title': ''}])
        return fixtures.probe(audio='dts', subs=subs, **kw)

    def test_cancel_clears_as_is_choice(self):
        app.ITEMS['m.mkv'] = {'name': 'm.mkv', 'status': 'queued', 'as_is': True}
        app.JOB_QUEUE.append('m.mkv')
        with self.request({'name': 'm.mkv'}):
            app.api_cancel()
        self.assertNotIn('as_is', app.ITEMS['m.mkv'])

    # ---- 升级只需 app.py 一个文件 ----
    def test_embedded_dockerfile_matches_repository_file(self):
        on_disk = (fixtures.APP_PATH.parent / 'Dockerfile').read_text('utf-8')
        self.assertEqual(app.EMBEDDED_DOCKERFILE, on_disk)
        extracted = app.extract_embedded_dockerfile(fixtures.APP_PATH.read_bytes())
        self.assertEqual(extracted.decode('utf-8'), on_disk)
        app.validate_upgrade_files(fixtures.APP_PATH.read_bytes(), extracted)

    def test_upgrade_accepts_app_py_alone(self):
        started = []
        class Thread:
            def __init__(self, target, args, daemon): started.append(args)
            def start(self): pass
        with patch.object(app.request, 'remote_addr', '192.168.1.21', create=True), \
                patch.object(app.request, 'files', {'app_py': io.BytesIO(fixtures.APP_PATH.read_bytes())}, create=True), \
                patch.object(app.threading, 'Thread', Thread):
            response, status = app.api_upgrade()
        self.assertEqual(status, 202, response)
        self.assertIn(b'pip install --no-cache-dir flask docker', started[0][1])
        self.assertIn(b'COPY app.py /app/app.py', started[0][1])

    def test_upgrade_with_app_lacking_embedded_dockerfile_is_refused(self):
        self.assertIsNone(app.extract_embedded_dockerfile(b"print('old version')\n"))
        self.assertIsNone(app.extract_embedded_dockerfile(b'EMBEDDED_DOCKERFILE = open("x").read()\n'))
        with patch.object(app.request, 'remote_addr', '192.168.1.21', create=True), \
                patch.object(app.request, 'files', {'app_py': io.BytesIO(b"print('old')\n")}, create=True), \
                patch.object(app.threading, 'Thread') as thread:
            response, status = app.api_upgrade()
        self.assertEqual(status, 400)
        self.assertIn('内嵌的 Dockerfile', response['error'])
        thread.assert_not_called()
        self.assertNotIn('upgradeDockerfile', app.PAGE)

    # ---- MakeMKV 结果汇总：成功时只有“已保存数”一个参数 ----
    def test_makemkv_success_summary_without_failed_count_is_accepted(self):
        log = ('MSG:5014,131072,2,"Saving 1 titles into directory file:///x",'
               '"Saving %1 titles into directory %2","1","file:///x"\n'
               'MSG:5036,260,1,"Copy complete. 1 titles saved.",'
               '"Copy complete. %1 titles saved.","1"\n')
        _, _, result = self.run_fake_rip(log=log)
        self.assertIsInstance(result, Path)

    def test_makemkv_start_message_alone_is_not_proof_of_success(self):
        log = ('MSG:5014,131072,2,"Saving 1 titles into directory file:///x",'
               '"Saving %1 titles into directory %2","1","file:///x"\n')
        _, _, result = self.run_fake_rip(log=log)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('拒绝使用残片', str(result))

    def test_makemkv_summary_with_failed_titles_is_rejected(self):
        log = ('MSG:5037,516,2,"Copy complete. 1 titles saved, 1 failed.",'
               '"Copy complete. %1 titles saved, %2 failed.","1","1"\n')
        _, _, result = self.run_fake_rip(log=log)
        self.assertIsInstance(result, RuntimeError)

    def test_makemkv_summary_with_zero_saved_is_rejected(self):
        log = ('MSG:5036,260,1,"Copy complete. 0 titles saved.",'
               '"Copy complete. %1 titles saved.","0"\n')
        _, _, result = self.run_fake_rip(log=log)
        self.assertIsInstance(result, RuntimeError)


    # ---- 整条识别被系统杀掉时，分段识别 ----
    @staticmethod
    def fake_sup(subtitles):
        def segment(kind, payload, pts):
            return b'PG' + pts.to_bytes(4, 'big') + bytes(4) + bytes([kind]) + len(payload).to_bytes(2, 'big') + payload
        out = b''
        for n in range(subtitles):
            pcs_show = bytes([7, 128, 4, 56, 16, 0, n % 256, 0x80, 0, 0, 1]) + bytes(8)
            pcs_clear = bytes([7, 128, 4, 56, 16, 0, n % 256, 0x00, 0, 0, 0])
            out += segment(0x16, pcs_show, n * 1000) + segment(0x15, bytes(40), n * 1000) + segment(0x80, b'', n * 1000)
            out += segment(0x16, pcs_clear, n * 1000 + 500) + segment(0x80, b'', n * 1000 + 500)
        return out


    def test_snapshot_recorded_by_previous_version_still_allows_deletion(self):
        source, output = self.finished_source(directory=True)
        app.PROCESSED[source.name]['source_snapshot'] = app.legacy_source_snapshot(source)
        self.assertNotEqual(app.legacy_source_snapshot(source), app.source_snapshot(source))
        with self.request({'name': source.name}):
            response = app.api_delete_source()
        self.assertEqual(response, {'ok': True})
        self.assertFalse(source.exists())
        self.assertTrue(output.exists())

    # ---- 完成后按实际 OCR 结果显示，而不是处理前的预计 ----
    def done_item_with_pgs(self, record):
        source, output = self.finished_source()
        media = fixtures.probe(subs=[{'codec': 'hdmv_pgs_subtitle', 'lang': 'jpn', 'title': ''}] * 9)
        # 旧版本（OCR 时代）保存下来的完成记录长这样
        planned = {'source': 'x', 'source_detail': 'y', 'target': 'z',
                   'target_detail': '画面直通 · ' + app.OCR_PLANNED_DETAIL}
        app.ITEMS[source.name].update(
            size=8, probe=media, payload=str(source), error=None,
            plan=['9条图形字幕OCR成文本'], clean=False)
        app.PROCESSED[source.name].update(format_preview=planned, **record)
        return next(i for i in app.api_state()['items'] if i['name'] == source.name)


    def test_multi_disc_preview_does_not_promise_a_probe(self):
        preview = app.format_preview('multi_disc', None, '')
        self.assertEqual(preview['target'], '需先拆分')
        self.assertNotIn('等待', preview['target'] + preview['source_detail'])
        self.assertIn("if(it.status==='blocked')return discardBtn(it)?", app.PAGE)

    # ---- 封面图不是视频轨 ----
    def test_cover_art_is_not_counted_as_a_video_track(self):
        raw = {'format': {'duration': '60'}, 'streams': [
            {'codec_type': 'video', 'codec_name': 'mjpeg', 'disposition': {'attached_pic': 1}},
            {'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080,
             'disposition': {'attached_pic': 0}},
            {'codec_type': 'audio', 'codec_name': 'eac3', 'channels': 6},
        ]}
        media = app.summarize_probe(raw)
        self.assertEqual(media['video_count'], 1)
        self.assertEqual(media['video']['codec'], 'h264')
        actions, clean = app.build_plan('file', media)
        self.assertTrue(clean, actions)

    # ---- 扫描：ffprobe 不占全局锁，失败后退避 ----
    def test_scan_probes_outside_the_global_lock_and_backs_off_on_failure(self):
        (self.inbox / 'broken.mkv').write_bytes(b'junk')
        calls = []
        def probing(_path):
            got = []
            worker = threading.Thread(
                target=lambda: got.append(app.LOCK.acquire(timeout=2) and (app.LOCK.release() or True)))
            worker.start(); worker.join()
            calls.append(bool(got and got[0]))
            return None
        with patch.object(app, 'ffprobe', side_effect=probing):
            for _ in range(5):
                app.scan_once()
        self.assertEqual(calls, [True])               # 只探测一次，且期间锁可被其他线程拿到
        self.assertEqual(app.ITEMS['broken.mkv']['status'], 'ready')
        app.PROBE_FAILURES['broken.mkv']['retry_at'] = 0
        with patch.object(app, 'ffprobe', return_value={}), \
                patch.object(app, 'summarize_probe', return_value=fixtures.probe()):
            app.scan_once()
        self.assertNotIn('broken.mkv', app.PROBE_FAILURES)
        self.assertIsNotNone(app.ITEMS['broken.mkv']['probe'])

    # ---- 源快照忽略系统杂项 ----
    def test_snapshot_ignores_nas_and_desktop_metadata_but_sees_real_changes(self):
        folder = self.inbox / 'movie'; folder.mkdir()
        (folder / 'movie.mkv').write_bytes(b'original')
        before = app.source_snapshot(folder)
        size_before = app.dir_size(folder)
        (folder / '.DS_Store').write_bytes(b'finder')
        (folder / '._movie.mkv').write_bytes(b'appledouble')
        (folder / '@eaDir').mkdir()
        (folder / '@eaDir' / 'movie.mkv@SynoEAStream').write_bytes(b'index')
        self.assertEqual(app.source_snapshot(folder), before)
        self.assertEqual(app.dir_size(folder), size_before)
        (folder / 'extra.nfo').write_text('new')
        self.assertNotEqual(app.source_snapshot(folder), before)
        (folder / 'extra.nfo').unlink()
        self.assertEqual(app.source_snapshot(folder), before)
        (folder / 'movie.mkv').write_bytes(b'replaced')
        self.assertNotEqual(app.source_snapshot(folder), before)

    # ---- 同名新下载不冒充已完成 ----
    def test_same_name_redownload_is_not_shown_as_finished(self):
        source, output = self.finished_source()
        app.PROCESSED[source.name]['source_size'] = source.stat().st_size
        app.PROCESSED[source.name]['warnings'] = ['旧警告']
        app.ITEMS.clear()
        with patch.object(app, 'ffprobe', return_value={}), \
                patch.object(app, 'summarize_probe', return_value=fixtures.probe()):
            app.scan_once(); app.scan_once()
            self.assertEqual(app.ITEMS[source.name]['status'], 'done')
            self.assertFalse(app.ITEMS[source.name]['source_changed'])
            source.write_bytes(b'a new download, still growing')
            app.scan_once()
            self.assertEqual(app.ITEMS[source.name]['status'], 'growing')
            growing = next(i for i in app.api_state()['items'] if i['name'] == source.name)
            self.assertIsNone(growing['output_name'])
            self.assertEqual(growing['warnings'], [])
            app.scan_once()
        item = app.ITEMS[source.name]
        self.assertEqual(item['status'], 'done')
        self.assertTrue(item['source_changed'])
        shown = next(i for i in app.api_state()['items'] if i['name'] == source.name)
        self.assertEqual(shown['warnings'][0], '旧警告')
        self.assertIn('大小与处理时不同', shown['warnings'][-1])
        self.assertEqual(app.PROCESSED[source.name]['warnings'], ['旧警告'])
        with self.request({'name': source.name}):
            _, status = app.api_delete_source()
        self.assertEqual(status, 409)
        self.assertTrue(source.exists())

    # ---- 升级助手消失后不再锁死面板 ----
    def stale_upgrade(self, status, age, helpers):
        app.write_upgrade_status(status, '进行中', upgrade_id='u1')
        saved = app.read_upgrade_status()
        saved['updated_epoch'] = app.time.time() - age
        app.upgrade_status_path().write_text(fixtures.json.dumps(saved), 'utf-8')
        class Client:
            containers = types.SimpleNamespace(list=lambda filters: helpers)
            def __enter__(self): return self
            def __exit__(self, *args): return False
        docker = types.SimpleNamespace(from_env=lambda: Client())
        with patch.dict(sys.modules, {'docker': docker}):
            app.recover_interrupted_upgrade(startup=False)
        return app.read_upgrade_status()['status']

    def test_stuck_upgrade_is_released_once_helper_is_gone(self):
        self.assertEqual(self.stale_upgrade('ready', 600, []), 'failed')
        self.assertEqual(app.processing_block_reason(), '')

    def test_running_or_fresh_upgrade_is_left_alone(self):
        self.assertEqual(self.stale_upgrade('ready', 5, []), 'ready')          # 助手还没创建出来
        self.assertEqual(self.stale_upgrade('switching', 600, ['helper']), 'switching')
        self.assertEqual(self.stale_upgrade('building', 600, []), 'building')  # 本进程的构建线程

    def test_upgrade_helper_reports_failure_during_preparation(self):
        compile(app.UPGRADE_HELPER, '<upgrade-helper>', 'exec')
        prepare, switch = app.UPGRADE_HELPER.split('new = None', 1)
        self.assertIn('client.containers.get(old_id)', prepare)
        self.assertIn('write("failed", f"升级准备失败', prepare)
        self.assertIn('raise SystemExit(1)', prepare)
        self.assertIn('old.stop(', switch)

    # ---- 跨站请求 / DNS 重绑定防护 ----
    def guard(self, host, method='POST', **headers):
        fake = types.SimpleNamespace(host=host, method=method, headers=headers)
        with patch.object(app, 'request', fake):
            return app.reject_cross_site_requests()

    def test_same_origin_and_non_browser_requests_pass(self):
        self.assertIsNone(self.guard('192.168.1.20:8097', Origin='http://192.168.1.20:8097'))
        self.assertIsNone(self.guard('192.168.1.20:8097',
                                     Referer='http://192.168.1.20:8097/'))
        self.assertIsNone(self.guard('127.0.0.1:8097'))            # curl / 升级健康检查
        self.assertIsNone(self.guard('diskstation:8097', Origin='http://diskstation:8097'))
        self.assertIsNone(self.guard('nas.local:8097', Origin='http://nas.local:8097'))
        self.assertIsNone(self.guard('[fe80::1]:8097', method='GET'))

    def test_cross_site_post_is_rejected(self):
        for origin in ('https://evil.example', 'null', 'http://192.168.1.20:9000'):
            result = self.guard('192.168.1.20:8097', Origin=origin)
            self.assertEqual(result[1], 403, origin)
        result = self.guard('192.168.1.20:8097', Referer='https://evil.example/page')
        self.assertEqual(result[1], 403)

    def test_cross_site_get_of_page_is_still_allowed(self):
        self.assertIsNone(self.guard('192.168.1.20:8097', method='GET',
                                     Referer='https://example.com/'))

    def test_dns_rebinding_host_is_rejected_unless_allowlisted(self):
        result = self.guard('evil.example:8097', method='GET')
        self.assertEqual(result[1], 403)
        result = self.guard('evil.example:8097', Origin='http://evil.example:8097')
        self.assertEqual(result[1], 403)
        self.assertEqual(self.guard('', method='GET')[1], 403)
        with patch.dict(os.environ, {'MEDIAPANEL_ALLOWED_HOSTS': 'nas.example.net, other.example'}):
            self.assertIsNone(self.guard('nas.example.net:8097',
                                         Origin='http://nas.example.net:8097'))
            self.assertEqual(self.guard('evil.example:8097', method='GET')[1], 403)


if __name__ == '__main__':
    unittest.main()
