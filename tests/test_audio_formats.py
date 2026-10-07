"""Real ffmpeg fixtures prove every promised direct-audio format is usable."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import mutagen
import scraper


class AudioFormatTests(unittest.TestCase):
    def test_every_supported_format_acquires_without_transcoding_and_is_recoverable(self):
        formats={'.m4a':'aac','.mp3':'libmp3lame','.aac':'aac','.opus':'libopus',
                 '.flac':'flac','.wav':'pcm_s16le'}
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for extension,codec in formats.items():
                with self.subTest(extension=extension):
                    source=root/('source'+extension)
                    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=330:duration=1',
                                    '-c:a',codec,'-y',str(source)],check=True)
                    before=scraper.validate_audio(source)
                    payload=source.read_bytes()
                    class Response:
                        def __enter__(self):return self
                        def __exit__(self,*args):pass
                        def iter_content(self,size):yield payload
                    class HTTP:
                        def get(self,*args,**kwargs):return Response()
                    asset={'key':'format:'+extension,'creator':'Fixture Creator','title':'[F4M] Quiet hypnosis',
                           'published':1791400000,'targets':json.dumps([['reddit_media','https://i.redd.it/audio'+extension]])}
                    cfg={'output_root':str(root/'media')}
                    output,url=scraper.save_asset(asset,cfg,HTTP())
                    after=scraper.validate_audio(output)
                    self.assertEqual(extension,output.suffix)
                    def codec_name(path):
                        result=subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_name',
                                               '-of','json',str(path)],capture_output=True,text=True,check=True)
                        return json.loads(result.stdout)['streams'][0]['codec_name']
                    self.assertEqual(codec_name(source),codec_name(output))
                    if extension=='.aac':self.assertEqual(payload,output.read_bytes())
                    self.assertAlmostEqual(float(before['format']['duration']),float(after['format']['duration']),delta=.05)
                    self.assertTrue(all(s['codec_type']=='audio' for s in after['streams']))
                    fingerprint=(output.stat().st_size,output.stat().st_mtime_ns)
                    again,_=scraper.save_asset(asset,cfg,HTTP())
                    self.assertEqual(output,again)
                    self.assertEqual(fingerprint,(output.stat().st_size,output.stat().st_mtime_ns))
                    if extension=='.wav':
                        tags=mutagen.File(output).tags
                        self.assertEqual(['Fixture Creator'],tags['TPE1'].text)
                        self.assertEqual(['Singles'],tags['TALB'].text)


if __name__=='__main__':unittest.main()
