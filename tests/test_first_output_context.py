import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from ork_codex import first_output_context as active
from ork_codex.first_output_context import core, packet
from ork_codex.first_output_context.core import SourcePolicy, collect, query
from ork_codex.first_output_context.packet import render
from ork_codex.auto_hook import configure
import plus_fixture


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.work=self.base/'work';self.work.mkdir()
        (self.work/'config.py').write_text('TENANT_RETENTION_18010 = 99\nTENANT_RETENTION_1801 = 2\n', newline='\n')
        (self.work/'consumer.py').write_text('from config import TENANT_RETENTION_1801\n', newline='\n')
        (self.work/'secrets.py').write_text('TENANT_RETENTION_1801 = "NEVER_DELIVER"\n', newline='\n')
        subprocess.run(['git','init','-q',str(self.work)],check=True)
        subprocess.run(['git','-C',str(self.work),'add','config.py','consumer.py','secrets.py'],check=True)
        self.transcript=self.base/'transcript.jsonl'
        self.write_transcript('Change tenant 1801 in config.py.')
        self.event={'hook_event_name':'PostToolUse','tool_name':'Bash',
            'session_id':'session-a','tool_use_id':'call-a','cwd':str(self.work),
            'transcript_path':str(self.transcript),'tool_input':{'command':'pwd'},
            'tool_response':{'output':'ORIGINAL\nexit_code=1'}}
        self.state=self.base/'state'
        self.env=patch.dict(os.environ,{'ORK_CODEX_MODE':'compact'});self.env.start();self.addCleanup(self.env.stop)

    def write_transcript(self,user):
        items=[{'type':'message','role':'user','content':[{'type':'input_text','text':user}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'I will update tenant 1801 to 17 days.'}]}]
        self.transcript.write_text(''.join(json.dumps({'type':'response_item','payload':i})+'\n' for i in items))

    def test_default_native_path_adds_exact_compact_ab(self):
        response,info,size=active.handle(self.event,self.state)
        self.assertTrue(response['reason'].startswith('ORIGINAL\nexit_code=1\n[source evidence]\n'))
        self.assertIn('2 TENANT_RETENTION_1801 = 2',response['reason'])
        self.assertNotIn('18010',response['reason']);self.assertNotIn('NEVER_DELIVER',response['reason'])
        self.assertLessEqual(info['source_evidence_tokens'],256)
        info.confirm(size)
        records=list((self.state/'first-output').glob('*.json'))
        self.assertEqual(json.loads(records[0].read_text())['status'],'emitted')

    def test_only_one_tool_result_per_session_is_enriched(self):
        active.handle(self.event,self.state)
        self.event['tool_use_id']='call-b'
        self.assertIsNone(active.handle(self.event,self.state))

    def test_replayed_original_and_already_enriched_output_do_not_duplicate(self):
        first=active.handle(self.event,self.state)
        replay=active.handle(self.event,self.state)
        self.assertEqual(first[0],replay[0])
        self.event['tool_response']['output']=first[0]['reason']
        self.assertIsNone(active.handle(self.event,self.state))
        self.assertEqual(self.event['tool_response']['output'].count('[source evidence]'),1)

    def test_restrictive_user_contract_prevents_any_source_read(self):
        self.write_transcript('Change tenant 1801. Do not inspect private.py.')
        with patch.object(core,'snapshot',side_effect=AssertionError('source opened')):
            self.assertIsNone(active.handle(self.event,self.state))

    def test_later_user_source_restrictions_are_honored(self):
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'Do not inspect config.py in this turn.'}]}})+'\n')
        with patch.object(core,'snapshot',side_effect=AssertionError('source opened')):
            self.assertIsNone(active.handle(self.event,self.state))

    def test_missing_transcript_cannot_authorize_project_scan(self):
        self.event.pop('transcript_path');self.event['tool_input']['command']="printf 'tenant 1801'"
        with patch.object(core,'snapshot',side_effect=AssertionError('source opened')):
            self.assertIsNone(active.handle(self.event,self.state))

    def test_missing_transcript_only_reads_literal_native_target(self):
        self.event.pop('transcript_path');self.event['tool_input']['command']='rg TENANT_RETENTION_1801 config.py'
        with patch.object(core,'snapshot',wraps=core.snapshot) as reads:
            response,_,_=active.handle(self.event,self.state)
        self.assertEqual([c.args[1] for c in reads.call_args_list],['config.py'])
        self.assertIn('TENANT_RETENTION_1801 = 2',response['reason'])

    def test_off_shadow_and_unsupported_tools_preserve_output(self):
        for mode in ('off','shadow'):
            with patch.dict(os.environ,{'ORK_CODEX_MODE':mode}):
                self.assertIsNone(active.handle(self.event,self.state))
        self.event['tool_name']='Other';self.assertIsNone(active.handle(self.event,self.state))

    @unittest.skipUnless(os.name == 'posix', 'symlink fixture requires POSIX')
    def test_ignored_sources_symlinks_and_secret_names_are_excluded(self):
        (self.work/'.gitignore').write_text('ignored.py\n')
        (self.work/'ignored.py').write_text('TENANT_RETENTION_1801 = "ignored"\n', newline='\n')
        (self.work/'linked.py').symlink_to(self.work/'secrets.py')
        with patch.object(core,'snapshot',wraps=core.snapshot) as reads:
            response,_,_=active.handle(self.event,self.state)
        self.assertFalse(any(c.args[1] in ('secrets.py','ignored.py') for c in reads.call_args_list))
        self.assertNotIn('NEVER_DELIVER',response['reason'])

    def test_unavailable_tokenizer_preserves_original_result(self):
        with patch.object(packet,'count',side_effect=ValueError('unavailable')):
            self.assertIsNone(active.handle(self.event,self.state))
        self.assertEqual(self.event['tool_response']['output'],'ORIGINAL\nexit_code=1')

    def test_current_main_calls_enrichment_without_feature_flag(self):
        name='ork_codex.auto_output_staged';module=type(sys)(name);module.__package__='ork_codex'
        exec(compile((ROOT/'ork_codex/auto_output.py').read_text(),str(ROOT/'ork_codex/auto_output.py'),'exec'),module.__dict__)
        with patch.object(module.entitlement,'refresh'),patch.object(module.entitlement,'entitled',return_value=True),\
             patch.dict(os.environ,{'PLUGIN_DATA':str(self.base)}),patch.object(sys,'stdin',io.StringIO(json.dumps(self.event))),\
             contextlib.redirect_stdout(io.StringIO()) as output:
            module.main()
        self.assertIn('[source evidence]',json.loads(output.getvalue())['reason'])

    def test_existing_reduction_has_priority(self):
        module=type(sys)('ork_codex.auto_output_priority');module.__package__='ork_codex'
        exec(compile((ROOT/'ork_codex/auto_output.py').read_text(),'auto_output_priority.py','exec'),module.__dict__)
        info=unittest.mock.Mock();info.get.return_value=None
        with patch.object(module.entitlement,'refresh'),patch.object(module.entitlement,'entitled',return_value=True),\
             patch.object(module,'handle',return_value=({'decision':'block','reason':'REDUCED'},info,7)),\
             patch.object(active,'handle',side_effect=AssertionError('reducer must have priority')),\
             patch.object(sys,'stdin',io.StringIO(json.dumps(self.event))),contextlib.redirect_stdout(io.StringIO()) as output:
            module.main()
        self.assertEqual(json.loads(output.getvalue())['reason'],'REDUCED')
        info.confirm.assert_called_once_with(7)

    def test_installed_snapshot_subprocess_applies_default_enrichment(self):
        home=self.base/'profile';home.mkdir();plus_fixture.write(home)
        configured=configure(home,'install');launch=Path(configured['hook']);bundle=launch.parent/'ork_codex'
        result=subprocess.run([sys.executable,'-I','-B',str(launch)],input=json.dumps(self.event),
            capture_output=True,text=True,env={**os.environ,'CODEX_HOME':str(home)},timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('[source evidence]',json.loads(result.stdout)['reason'])




class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'config.py').write_text('TENANT_RETENTION_18010 = 99\nTENANT_RETENTION_1801 = 2\n', newline='\n')
        (self.root/'consumer.py').write_text('from config import TENANT_RETENTION_1801\n', newline='\n')
        (self.root/'private.py').write_text('TENANT_RETENTION_1801 = "DO_NOT_READ"\n', newline='\n')
        self.policy = SourcePolicy(('config.py', 'consumer.py'))
        self.target = query('I will update tenant 1801 to 17 days.')

    def test_deny_and_unlisted_files_are_never_opened(self):
        policy = SourcePolicy(('config.py','private.py'), ('private.py',))
        with patch.object(core, 'snapshot', wraps=core.snapshot) as read:
            packet = collect(self.root, self.target, policy)
        self.assertEqual([c.args[1] for c in read.call_args_list], ['config.py'])
        self.assertNotIn('DO_NOT_READ', json.dumps(packet))
        self.assertEqual(packet['files_examined'], 1)

    def test_denied_directory_prefix_is_not_read(self):
        policy = SourcePolicy(('secret/a.py','config.py'), ('secret',))
        with patch.object(core, 'snapshot', wraps=core.snapshot) as read:
            collect(self.root,self.target,policy)
        self.assertEqual([c.args[1] for c in read.call_args_list], ['config.py'])

    def test_empty_access_policy_reads_nothing(self):
        with patch.object(core,'snapshot',side_effect=AssertionError('unexpected source read')):
            packet=collect(self.root,self.target,SourcePolicy(()))
        self.assertEqual(packet['files_examined'],0)
        self.assertIsNone(render(packet)[0])

    @unittest.skipUnless(os.name == 'posix', 'symlink fixture requires POSIX')
    def test_symlink_files_and_directories_are_never_followed(self):
        (self.root/'linked.py').symlink_to(self.root/'private.py')
        (self.root/'linked-dir').symlink_to(self.root, target_is_directory=True)
        packet=collect(self.root,self.target,SourcePolicy(('linked.py','linked-dir/private.py')))
        self.assertFalse(any(packet['sources'].values()))
        self.assertEqual(packet['bytes_read'],0)

    def test_ab_exact_entity_and_reference_locations(self):
        packet=collect(self.root,self.target,self.policy)
        text,receipt=render(packet)
        self.assertIn('2 TENANT_RETENTION_1801 = 2',text)
        self.assertNotIn('18010',text)
        self.assertLessEqual(receipt['tokens'],256)
        definition=packet['sources']['definitions'][0]
        self.assertEqual(definition['start_line'],2)
        self.assertEqual(definition['sha256'],hashlib.sha256((self.root/'config.py').read_bytes()).hexdigest())
        references={r['path']:r['locations'] for r in packet['sources']['reference_files']}
        self.assertEqual(references,{'config.py':[2],'consumer.py':[1]})

    def test_unknown_entity_does_not_fallback_to_generic_words(self):
        packet=collect(self.root,query('Update tenant 1802 in config.py'),self.policy)
        self.assertFalse(any(packet['sources'].values()))

    def test_sources_over_two_mib_are_read_to_eof(self):
        raw='#'+('x'*(2*1024*1024))+'\nTENANT_RETENTION_1801 = 2\n'
        (self.root/'config.py').write_text(raw, newline='\n')
        packet=collect(self.root,self.target,SourcePolicy(('config.py',)))
        self.assertEqual(packet['bytes_read'],len(raw.encode()))
        self.assertIn('TENANT_RETENTION_1801 = 2',render(packet)[0])

    def test_visible_exact_source_is_not_repeated(self):
        packet=collect(self.root,self.target,SourcePolicy(('config.py',)))
        self.assertIsNone(render(packet,['config.py\n2 TENANT_RETENTION_1801 = 2\n'])[0])
        self.assertIsNotNone(render(packet,['config.py'])[0])

    def test_long_indivisible_lines_never_force_over_budget_delivery(self):
        (self.root/'config.py').write_text('TENANT_RETENTION_1801 = '+repr('longword '*10000)+'\n', newline='\n')
        text,receipt=render(collect(self.root,self.target,SourcePolicy(('config.py',))))
        self.assertTrue(text is None or receipt['tokens'] <= 256)
        if text is not None:
            self.assertLess(len(text),1000)

    def test_small_packets_are_not_padded(self):
        text,receipt=render(collect(self.root,self.target,SourcePolicy(('config.py',))))
        self.assertLess(receipt['tokens'],64)
        self.assertTrue(text.endswith('TENANT_RETENTION_1801 = 2\n'))

if __name__=='__main__':unittest.main()
