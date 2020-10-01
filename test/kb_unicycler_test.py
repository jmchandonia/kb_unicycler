from __future__ import print_function
import unittest
import os
import time
import json

from os import environ
from configparser import ConfigParser
import psutil
from pprint import pprint
import shutil
import inspect
import requests

from installed_clients.AbstractHandleClient import AbstractHandle as HandleService
from kb_unicycler.kb_unicyclerImpl import kb_unicycler
from installed_clients.ReadsUtilsClient import ReadsUtils
from kb_unicycler.kb_unicyclerServer import MethodContext
from installed_clients.WorkspaceClient import Workspace

class unicyclerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.token = environ.get('KB_AUTH_TOKEN')
        cls.callbackURL = environ.get('SDK_CALLBACK_URL')
        print('CB URL: ' + cls.callbackURL)
        # WARNING: don't call any logging methods on the context object,
        # it'll result in a NoneType error
        cls.ctx = MethodContext(None)
        cls.ctx.update({'token': cls.token,
                        'provenance': [
                            {'service': 'kb_unicycler',
                             'method': 'please_never_use_it_in_production',
                             'method_params': []
                             }],
                        'authenticated': 1})
        config_file = environ.get('KB_DEPLOYMENT_CONFIG', None)
        cls.cfg = {}
        config = ConfigParser()
        config.read(config_file)
        for nameval in config.items('kb_unicycler'):
            cls.cfg[nameval[0]] = nameval[1]
        cls.cfg["SDK_CALLBACK_URL"] = cls.callbackURL
        cls.cfg["KB_AUTH_TOKEN"] = cls.token
        cls.wsURL = cls.cfg['workspace-url']
        cls.shockURL = cls.cfg['shock-url']
        cls.hs = HandleService(url=cls.cfg['handle-service-url'],
                               token=cls.token)
        # cls.wsClient = workspaceService(cls.wsURL, token=cls.token)
        cls.wsClient = Workspace(cls.wsURL, token=cls.token)
        wssuffix = int(time.time() * 1000)
        wsName = "test_kb_unicycler_" + str(wssuffix)
        cls.wsinfo = cls.wsClient.create_workspace({'workspace': wsName})
        print('created workspace ' + cls.getWsName())

        cls.PROJECT_DIR = 'unicycler_outputs'
        cls.scratch = cls.cfg['scratch']
        if not os.path.exists(cls.scratch):
            os.makedirs(cls.scratch)
        cls.prjdir = os.path.join(cls.scratch, cls.PROJECT_DIR)
        if not os.path.exists(cls.prjdir):
            os.makedirs(cls.prjdir)
        cls.serviceImpl = kb_unicycler(cls.cfg)

        cls.readUtilsImpl = ReadsUtils(cls.callbackURL, token=cls.token)
        cls.staged = {}
        cls.nodes_to_delete = []
        cls.handles_to_delete = []
        cls.setupTestData()
        print('\n\n=============== Starting Unicycler tests ==================')

    @classmethod
    def tearDownClass(cls):

        print('\n\n=============== Cleaning up ==================')

        if hasattr(cls, 'wsinfo'):
            cls.wsClient.delete_workspace({'workspace': cls.getWsName()})
            print('Test workspace was deleted: ' + cls.getWsName())
        if hasattr(cls, 'nodes_to_delete'):
            for node in cls.nodes_to_delete:
                cls.delete_shock_node(node)
        if hasattr(cls, 'handles_to_delete'):
            if cls.handles_to_delete:
                cls.hs.delete_handles(cls.hs.hids_to_handles(cls.handles_to_delete))
                print('Deleted handles ' + str(cls.handles_to_delete))

    @classmethod
    def getWsName(cls):
        return cls.wsinfo[1]

    def getImpl(self):
        return self.serviceImpl

    @classmethod
    def delete_shock_node(cls, node_id):
        header = {'Authorization': 'Oauth {0}'.format(cls.token)}
        requests.delete(cls.shockURL + '/node/' + node_id, headers=header,
                        allow_redirects=True)
        print('Deleted shock node ' + node_id)

    # Helper script borrowed from the transform service, logger removed
    @classmethod
    def upload_file_to_shock(cls, file_path):
        """
        Use HTTP multi-part POST to save a file to a SHOCK instance.
        """

        header = dict()
        header["Authorization"] = "Oauth {0}".format(cls.token)

        if file_path is None:
            raise Exception("No file given for upload to SHOCK!")

        with open(os.path.abspath(file_path), 'rb') as dataFile:
            files = {'upload': dataFile}
            print('POSTing data')
            response = requests.post(
                cls.shockURL + '/node', headers=header, files=files,
                stream=True, allow_redirects=True)
            print('got response')

        if not response.ok:
            response.raise_for_status()

        result = response.json()

        if result['error']:
            raise Exception(result['error'][0])
        else:
            return result["data"]

    @classmethod
    def upload_file_to_shock_and_get_handle(cls, test_file):
        '''
        Uploads the file in test_file to shock and returns the node and a
        handle to the node.
        '''
        print('loading file to shock: ' + test_file)
        node = cls.upload_file_to_shock(test_file)
        pprint(node)
        cls.nodes_to_delete.append(node['id'])

        print('creating handle for shock id ' + node['id'])
        handle_id = cls.hs.persist_handle({'id': node['id'],
                                           'type': 'shock',
                                           'url': cls.shockURL
                                           })
        cls.handles_to_delete.append(handle_id)

        md5 = node['file']['checksum']['md5']
        return node['id'], handle_id, md5, node['file']['size']

    @classmethod
    def upload_reads(cls, wsobjname, object_body, fwd_reads,
                     rev_reads=None, single_end=False, sequencing_tech='Illumina',
                     single_genome='1'):

        ob = dict(object_body)  # copy
        ob['sequencing_tech'] = sequencing_tech
        ob['wsname'] = cls.getWsName()
        ob['name'] = wsobjname
        if single_end or rev_reads:
            ob['interleaved'] = 0
        else:
            ob['interleaved'] = 1
        print('\n===============staging data for object ' + wsobjname +
              '================')
        print('uploading forward reads file ' + fwd_reads['file'])
        fwd_id, fwd_handle_id, fwd_md5, fwd_size = \
            cls.upload_file_to_shock_and_get_handle(fwd_reads['file'])

        ob['fwd_id'] = fwd_id
        rev_id = None
        rev_handle_id = None
        if rev_reads:
            print('uploading reverse reads file ' + rev_reads['file'])
            rev_id, rev_handle_id, rev_md5, rev_size = \
                cls.upload_file_to_shock_and_get_handle(rev_reads['file'])
            ob['rev_id'] = rev_id
        obj_ref = cls.readUtilsImpl.upload_reads(ob)
        objdata = cls.wsClient.get_object_info_new({
            'objects': [{'ref': obj_ref['obj_ref']}]
            })[0]
        cls.staged[wsobjname] = {'info': objdata,
                                 'ref': cls.make_ref(objdata),
                                 'fwd_node_id': fwd_id,
                                 'rev_node_id': rev_id,
                                 'fwd_handle_id': fwd_handle_id,
                                 'rev_handle_id': rev_handle_id
                                 }

    @classmethod
    def upload_assembly(cls, wsobjname, object_body, fwd_reads,
                        rev_reads=None, kbase_assy=False,
                        single_end=False, sequencing_tech='Illumina'):
        if single_end and rev_reads:
            raise ValueError('u r supr dum')

        print('\n===============staging data for object ' + wsobjname +
              '================')
        print('uploading forward reads file ' + fwd_reads['file'])
        fwd_id, fwd_handle_id, fwd_md5, fwd_size = \
            cls.upload_file_to_shock_and_get_handle(fwd_reads['file'])
        fwd_handle = {
                      'hid': fwd_handle_id,
                      'file_name': fwd_reads['name'],
                      'id': fwd_id,
                      'url': cls.shockURL,
                      'type': 'shock',
                      'remote_md5': fwd_md5
                      }

        ob = dict(object_body)  # copy
        ob['sequencing_tech'] = sequencing_tech
        if kbase_assy:
            if single_end:
                wstype = 'KBaseAssembly.SingleEndLibrary'
                ob['handle'] = fwd_handle
            else:
                wstype = 'KBaseAssembly.PairedEndLibrary'
                ob['handle_1'] = fwd_handle
        else:
            if single_end:
                wstype = 'KBaseFile.SingleEndLibrary'
                obkey = 'lib'
            else:
                wstype = 'KBaseFile.PairedEndLibrary'
                obkey = 'lib1'
            ob[obkey] = \
                {'file': fwd_handle,
                 'encoding': 'UTF8',
                 'type': fwd_reads['type'],
                 'size': fwd_size
                 }

        rev_id = None
        rev_handle_id = None
        if rev_reads:
            print('uploading reverse reads file ' + rev_reads['file'])
            rev_id, rev_handle_id, rev_md5, rev_size = \
                cls.upload_file_to_shock_and_get_handle(rev_reads['file'])
            rev_handle = {
                          'hid': rev_handle_id,
                          'file_name': rev_reads['name'],
                          'id': rev_id,
                          'url': cls.shockURL,
                          'type': 'shock',
                          'remote_md5': rev_md5
                          }
            if kbase_assy:
                ob['handle_2'] = rev_handle
            else:
                ob['lib2'] = \
                    {'file': rev_handle,
                     'encoding': 'UTF8',
                     'type': rev_reads['type'],
                     'size': rev_size
                     }

        print('Saving object data')
        objdata = cls.wsClient.save_objects({
            'workspace': cls.getWsName(),
            'objects': [
                        {
                         'type': wstype,
                         'data': ob,
                         'name': wsobjname
                         }]
            })[0]
        print('Saved object objdata: ')
        pprint(objdata)
        print('Saved object ob: ')
        pprint(ob)
        cls.staged[wsobjname] = {'info': objdata,
                                 'ref': cls.make_ref(objdata),
                                 'fwd_node_id': fwd_id,
                                 'rev_node_id': rev_id,
                                 'fwd_handle_id': fwd_handle_id,
                                 'rev_handle_id': rev_handle_id
                                 }

    @classmethod
    def upload_empty_data(cls, wsobjname):
        objdata = cls.wsClient.save_objects({
            'workspace': cls.getWsName(),
            'objects': [{'type': 'Empty.AType',
                         'data': {},
                         'name': 'empty'
                         }]
            })[0]
        cls.staged[wsobjname] = {'info': objdata,
                                 'ref': cls.make_ref(objdata),
                                 }

    @classmethod
    def setupTestData(cls):
        print('Shock url ' + cls.shockURL)
        # print('WS url ' + cls.wsClient.url)
        # print('Handle service url ' + cls.hs.url)
        print('CPUs detected ' + str(psutil.cpu_count()))
        print('Available memory ' + str(psutil.virtual_memory().available))
        print('staging data')

        # get file type from type
        fwd_reads = {'file': 'data/small.forward.fq',
                     'name': 'test_fwd.fastq',
                     'type': 'fastq'}
        # get file type from handle file name
        rev_reads = {'file': 'data/small.reverse.fq',
                     'name': 'test_rev.FQ',
                     'type': ''}
        # get file type from shock node file name
        int_reads = {'file': 'data/interleaved.fq',
                     'name': '',
                     'type': ''}
        int64_reads = {'file': 'data/interleaved64.fq',
                       'name': '',
                       'type': ''}
        pacbio_reads = {'file': 'data/pacbio_filtered_small.fastq.gz',
                        'name': '',
                        'type': ''}
        pacbio_ccs_reads = {'file': 'data/pacbioCCS_small.fastq.gz',
                            'name': '',
                            'type': ''}
        iontorrent_reads = {'file': 'data/IonTorrent_single.fastq.gz',
                            'name': '',
                            'type': ''}
        plasmid1_reads = {'file': 'data/pl1.fq.gz',
                          'name': '',
                          'type': ''}
        plasmid2_reads = {'file': 'data/pl2.fq.gz',
                          'name': '',
                          'type': ''}
        cls.upload_reads('frbasic', {}, fwd_reads, rev_reads=rev_reads)
        cls.upload_reads('intbasic', {'single_genome': 1}, int_reads)
        cls.upload_reads('intbasic64', {'single_genome': 1}, int64_reads)
        cls.upload_reads('pacbio', {'single_genome': 1}, pacbio_reads,
                         single_end=True, sequencing_tech="PacBio CLR")
        cls.upload_reads('pacbioccs', {'single_genome': 1}, pacbio_ccs_reads,
                         single_end=True, sequencing_tech="PacBio CCS")
        cls.upload_reads('iontorrent', {'single_genome': 1}, iontorrent_reads,
                         single_end=True, sequencing_tech="IonTorrent")
        cls.upload_reads('reads_out', {'read_orientation_outward': 1}, int_reads)
        cls.upload_assembly('frbasic_kbassy', {}, fwd_reads, rev_reads=rev_reads, kbase_assy=True)
        cls.upload_assembly('intbasic_kbassy', {}, int_reads, kbase_assy=True)
        cls.upload_reads('single_end', {}, fwd_reads, single_end=True)
        cls.upload_reads('single_end2', {}, rev_reads, single_end=True)
        cls.upload_reads('plasmid_reads', {'single_genome': 1},
                         plasmid1_reads, rev_reads=plasmid2_reads)
        shutil.copy2('data/small.forward.fq', 'data/small.forward.bad')
        bad_fn_reads = {'file': 'data/small.forward.bad',
                        'name': '',
                        'type': ''}
        cls.upload_assembly('bad_shk_name', {}, bad_fn_reads)
        bad_fn_reads['file'] = 'data/small.forward.fq'
        bad_fn_reads['name'] = 'file.terrible'
        cls.upload_assembly('bad_file_name', {}, bad_fn_reads)
        bad_fn_reads['name'] = 'small.forward.fastq'
        bad_fn_reads['type'] = 'xls'
        cls.upload_assembly('bad_file_type', {}, bad_fn_reads)
        cls.upload_assembly('bad_node', {}, fwd_reads)
        cls.delete_shock_node(cls.nodes_to_delete.pop())
        cls.upload_empty_data('empty')
        print('Data staged.')

    @classmethod
    def make_ref(self, object_info):
        return str(object_info[6]) + '/' + str(object_info[0]) + \
            '/' + str(object_info[4])

    def run_unicycler(self,
                      short_paired_libraries,
                      output_contigset_name,
                      short_unpaired_libraries=None,
                      long_reads_library=None,
                      min_contig_length=100,
                      num_linear_seqs=0,
                      bridging_mode="normal"):
        """
        run_unicycler: The main method to test all possible input data sets
        """
        test_name = inspect.stack()[1][3]
        print('\n**** starting expected success test: ' + test_name + ' *****')
        print('   libs: ' + str(readnames))

        print("SHORT_PAIRED: " + str(short_paired_libraries))
        print("SHORT_UNPAIRED: " + str(short_unpaired_libraries))
        print("LONG: " + str(long_reads_library))
        print("STAGED: " + str(self.staged))

        params = {'workspace_name': self.getWsName(),
                  'short_paired_libraries': short_paired_libraries,
                  'short_unpaired_libraries': short_unpaired_libraries,
                  'long_reads_library': long_reads_library,
                  'output_contigset_name': output_contigset_name,
                  'min_contig_length': min_contig_length,
                  'num_linear_seqs': num_linear_seqs,
                  'bridging_mode': bridging_mode
                  }

        ret = self.getImpl().run_Unicycler(self.ctx, params)[0]
        if params.get('create_report', 0) == 1:
            self.assertReportAssembly(ret, output_contigset_name)

    def assertReportAssembly(self, ret_obj, assembly_name):
        """
        assertReportAssembly: given a report object, check the object existence
        """
        report = self.wsClient.get_objects2({
                        'objects': [{'ref': ret_obj['report_ref']}]})['data'][0]
        self.assertEqual('KBaseReport.Report', report['info'][2].split('-')[0])
        self.assertEqual(1, len(report['data']['objects_created']))
        self.assertEqual('Assembled contigs',
                         report['data']['objects_created'][0]['description'])
        self.assertIn('Assembled into ', report['data']['text_message'])
        self.assertIn('contigs', report['data']['text_message'])
        print("**************Report Message*************\n")
        print(report['data']['text_message'])

        assembly_ref = report['data']['objects_created'][0]['ref']
        assembly = self.wsClient.get_objects([{'ref': assembly_ref}])[0]

        self.assertEqual('KBaseGenomeAnnotations.Assembly', assembly['info'][2].split('-')[0])
        self.assertEqual(1, len(assembly['provenance']))
        self.assertEqual(assembly_name, assembly['data']['assembly_id'])

        temp_handle_info = self.hs.hids_to_handles([assembly['data']['fasta_handle_ref']])
        assembly_fasta_node = temp_handle_info[0]['id']
        self.nodes_to_delete.append(assembly_fasta_node)

    # Uncomment to skip this test
    # @unittest.skip("skipped test_fr_pair_kbfile")
    def test_fr_pair_kbfile(self):
        self.run_hybrid_success(
            ['frbasic'], 'frbasic_out')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_fr_pair_kbassy")
    def test_fr_pair_kbassy(self):
        self.run_hybrid_success(
            ['frbasic_kbassy'], 'frbasic_kbassy_out')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_interlaced_kbfile")
    def test_interlaced_kbfile(self):
        self.run_hybrid_success(
            ['intbasic'], 'intbasic_out')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_interlaced_kbassy")
    def test_interlaced_kbassy(self):
        self.run_hybrid_success(
            ['intbasic_kbassy'], 'intbasic_kbassy_out',
            dna_source='')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_multiple")
    def test_multiple(self):
        self.run_hybrid_success(
            ['intbasic_kbassy', 'frbasic'], 'multiple_out',
            dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_plasmid_kbfile")
    def test_plasmid_kbfile(self):
        self.run_hybrid_success(
            ['plasmid_reads'], 'plasmid_out',
            dna_source='plasmid')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_multiple_single")
    def test_multiple_single(self):
        self.run_hybrid_success(
            ['single_end', 'single_end2'], 'multiple_single_out',
            dna_source='None', lib_type='single')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_multiple_pacbio_single")
    def test_multiple_pacbio_single(self):
        self.run_hybrid_success(
            ['single_end'], 'pacbio_single_out', longreadnames=['pacbio'],
            lib_type='single', long_reads_type='pacbio_clr', dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_multiple_pacbio_illumina")
    def test_multiple_pacbio_illumina(self):
        self.run_hybrid_success(
            ['intbasic_kbassy'], 'pacbio_multiple_out', longreadnames=['pacbio'],
            lib_type='single', long_reads_type='pacbio_clr', dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_pacbioccs_alone")
    def test_pacbioccs_alone(self):
        self.run_hybrid_success(
            ['pacbioccs'], 'pacbioccs_alone_out', lib_type='single',
            dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_pacbioccs_single")
    def test_pacbioccs_single(self):
        self.run_hybrid_success(
            ['single_end'], 'pacbioccs_single_out', longreadnames=['pacbioccs'],
            lib_type='single', long_reads_type='pacbio_ccs', dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_multiple_pacbioccs_illumina")
    def test_multiple_pacbioccs_illumina(self):
        self.run_hybrid_success(
            ['intbasic_kbassy'], 'pacbioccs_multiple_out', longreadnames=['pacbioccs'],
            lib_type='single', long_reads_type='pacbio_ccs', dna_source='None')

    # Uncomment to skip this test
    # @unittest.skip("skipped test_single_reads")
    def test_single_reads(self):
        self.run_hybrid_success(
            ['single_end'], 'single_out',
            lib_type='single', dna_source='None')

    # ########################End of passed tests######################