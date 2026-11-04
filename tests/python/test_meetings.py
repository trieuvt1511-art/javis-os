"""Meeting persistence, reference integrity and API failure boundaries."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'server'))
from meeting_store import MeetingStore, atomic
import meeting_routes
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

class Meetings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MeetingStore(self.temp.name)
        self.ident = self.store.create('Test', 'vi', 'audio/wav')['id']

    def saved(self):
        self.store.append(self.ident, 0, b'fixture-audio')
        return self.store.finish(self.ident)

    def transcript(self):
        self.saved()
        atomic(self.store.folder(self.ident)/'transcript.json', {'segments':[dict(id=1,start=1.2,end=3.4,text='Test')]})

    def test_duplicate_chunk_retry_and_restart(self):
        self.store.append(self.ident,0,b'abc')
        self.assertEqual(self.store.append(self.ident,0,b'abc')['bytes'],3)
        other=MeetingStore(self.temp.name)
        self.assertEqual(other.read(self.ident)['chunks'],1)
        other.finish(self.ident)
        self.assertEqual((other.folder(self.ident)/'audio.wav').read_bytes(),b'abc')

    def test_reordered_chunk_rejected(self):
        with self.assertRaises(ValueError):self.store.append(self.ident,1,b'abc')

    def test_changed_retry_rejected(self):
        self.store.append(self.ident,0,b'abc')
        with self.assertRaises(ValueError):self.store.append(self.ident,0,b'xyz')

    def test_empty_and_finished_recordings(self):
        with self.assertRaises(ValueError):self.store.finish(self.ident)
        self.saved()
        with self.assertRaises(ValueError):self.store.append(self.ident,1,b'x')

    def test_limit_and_traversal(self):
        with patch('meeting_store.MAX_BYTES',2):
            with self.assertRaises(ValueError):self.store.append(self.ident,0,b'abc')
        for ident in ['../secret', '/', 'invalid']:
            with self.assertRaises(ValueError):self.store.folder(ident)

    def test_summary_requires_real_references(self):
        self.transcript()
        with self.assertRaises(ValueError):
            self.store.save_minutes(self.ident,dict(topics=[dict(text='Claim',segments=[99])],decisions=[],actions=[]))
        self.assertFalse((self.store.folder(self.ident)/'minutes.md').exists())

    def test_minutes_include_timestamps_and_unknown_fields(self):
        self.transcript()
        self.store.save_minutes(self.ident,dict(topics=[],decisions=[],actions=[dict(text='Kiểm tra',segments=[1])]))
        text=(self.store.folder(self.ident)/'minutes.md').read_text()
        self.assertIn('00:00:01.200 - 00:00:03.400',text)
        self.assertIn('Người phụ trách: chưa xác định; Hạn: chưa xác định',text)

    def test_missing_model_preserves_audio_and_reports_error(self):
        self.saved()
        with self.assertRaises(ValueError):self.store.transcribe(self.ident,Path(self.temp.name)/'missing.bin')
        self.assertEqual(self.store.read(self.ident)['state'],'error')
        self.assertTrue((self.store.folder(self.ident)/'audio.wav').exists())

    def test_api_files_errors_and_summary(self):
        async def scenario():
            app=FastAPI()
            async def summary(transcript):return dict(topics=[dict(text='Nội dung',segments=[1])],decisions=[],actions=[])
            meeting_routes.register(app,lambda _:self.temp.name,summary)
            self.transcript()
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                self.assertEqual((await client.get(f'/meetings/{self.ident}/files/status.json')).status_code,404)
                self.assertEqual((await client.post(f'/meetings/{self.ident}/chunks/-1',content=b'x')).status_code,400)
                self.assertEqual((await client.post(f'/meetings/{self.ident}/summarize')).status_code,200)
                await asyncio.sleep(.05)
                self.assertEqual((await client.get(f'/meetings/{self.ident}')).json()['state'],'complete')
                self.assertEqual((await client.get(f'/meetings/{self.ident}/files/minutes.md')).status_code,200)
        asyncio.run(scenario())

    def test_interrupted_processing_recoverable(self):
        async def scenario():
            app=FastAPI()
            meeting_routes.register(app,lambda _:self.temp.name,None)
            self.saved();self.store.update(self.ident,state='transcribing')
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                row=(await client.get('/meetings')).json()[0]
                self.assertEqual(row['state'],'error')
                self.assertIn('Interrupted',row['error'])
        asyncio.run(scenario())

if __name__=='__main__':unittest.main()
