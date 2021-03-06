# -*- coding: utf-8 -*-
"""
Copyright (c) 2017 beyond-blockchain.org.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import gevent
from gevent import monkey
monkey.patch_all()
from gevent import socket
import json
import traceback
import queue
import binascii
import os

import sys
sys.path.append("../../")

from bbc1.common import bbclib, message_key_types
from bbc1.common.bbclib import NodeInfo, ServiceMessageType as MsgType, StorageType
from bbc1.common.message_key_types import KeyType, PayloadType
from bbc1.common.bbc_error import *
from bbc1.common import logger

DEFAULT_CORE_PORT = 9000
MAPPING_FILE = ".bbc_id_mappings"


def store_id_mappings(name, asset_group_id, transaction_id=None, asset_ids=None):
    if transaction_id is None and asset_ids is None:
        return
    mapping = dict()
    asset_group_id_str = binascii.b2a_hex(asset_group_id).decode()
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r") as f:
            mapping = json.load(f)

    mapping.setdefault(asset_group_id_str, dict()).setdefault(name, dict())
    if transaction_id is not None:
        mapping[asset_group_id_str][name]['transaction_id'] = binascii.b2a_hex(transaction_id).decode()
    if asset_ids is not None:
        if isinstance(asset_ids, list):
            entry = []
            for ast in asset_ids:
                entry.append(binascii.b2a_hex(ast))
            mapping[asset_group_id_str][name]['asset_id'] = entry
        else:
            mapping[asset_group_id_str][name]['asset_id'] = binascii.b2a_hex(asset_ids).decode()

    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=4)


def remove_id_mappings(name, asset_group_id):
    mapping = dict()
    asset_group_id_str = binascii.b2a_hex(asset_group_id).decode()
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r") as f:
            mapping = json.load(f)
    if asset_group_id_str in mapping:
        mapping[asset_group_id_str].pop(name, None)
        if len(mapping[asset_group_id_str].keys()) == 0:
            del mapping[asset_group_id_str]

    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=4)


def get_id_from_mappings(name, asset_group_id):
    if not os.path.exists(MAPPING_FILE):
        return None
    asset_group_id_str = binascii.b2a_hex(asset_group_id).decode()
    with open(MAPPING_FILE, "r") as f:
        mapping = json.load(f)
    if mapping is None:
        return None
    if asset_group_id_str in mapping and name in mapping[asset_group_id_str]:
        result = dict()
        if 'transaction_id' in mapping[asset_group_id_str][name]:
            result['transaction_id'] = binascii.a2b_hex(mapping[asset_group_id_str][name]['transaction_id'])
        if 'asset_id' in mapping[asset_group_id_str][name]:
            if isinstance(mapping[asset_group_id_str][name]['asset_id'], list):
                entry = []
                for ast in mapping[asset_group_id_str][name]['asset_id']:
                    entry.append(binascii.a2b_hex(ast))
                result['asset_id'] = entry
            else:
                result['asset_id'] = binascii.a2b_hex(mapping[asset_group_id_str][name]['asset_id'])
        return result
    return None


class BBcAppClient:
    def __init__(self, host='127.0.0.1', port=DEFAULT_CORE_PORT, logname="-", loglevel="none"):
        self.logger = logger.get_logger(key="bbc_app", level=loglevel, logname=logname)
        self.connection = socket.create_connection((host, port))
        self.callback = Callback(log=self.logger)
        self.asset_groups = set()
        self.user_id = None
        self.query_id = (0).to_bytes(2, 'little')
        self.start_receiver_loop()

    def set_callback(self, callback_obj):
        """
        Set callback object that implements message processing functions

        :param callback_obj:
        :return:
        """
        self.callback = callback_obj
        self.callback.set_logger(self.logger)

    def set_user_id(self, identifier):
        """
        Set user_id of the object

        :param identifier:
        :return:
        """
        self.user_id = identifier

    def set_asset_group_id(self, asset_group_id):
        """
        Set asset_group_id (before register_to_core)

        :param asset_group_id:
        :return:
        """
        self.asset_groups.add(asset_group_id)

    def make_message_structure(self, asset_group_id, cmd):
        """
        (internal use) make a base message structure for sending to the core node

        :param asset_group_id:
        :param cmd:
        :return:
        """
        self.query_id = ((int.from_bytes(self.query_id, 'little') + 1) // 65536).to_bytes(2, 'little')
        return {
            KeyType.command: cmd,
            KeyType.asset_group_id: asset_group_id,
            KeyType.source_user_id: self.user_id,
            KeyType.query_id: self.query_id,
            KeyType.status: ESUCCESS,
        }

    def send_msg(self, dat):
        """
        (internal use) send the message to the core node

        :param dat:
        :return:
        """
        if KeyType.asset_group_id not in dat or KeyType.source_user_id not in dat:
            self.logger.warn("Message must include asset_group_id and source_id")
            return False
        try:
            msg = message_key_types.make_message(PayloadType.Type_msgpack, dat)
            self.connection.sendall(msg)
        except Exception as e:
            self.logger.error(e)
            return False
        return True

    def domain_setup(self, domain_id, module_name=None):
        """
        Set up domain with the specified network module (maybe used by a system administrator)

        :param domain_id:
        :param module_name:
        :param storage_type: StorageType value
        :param storage_path:
        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_SETUP_DOMAIN)
        dat[KeyType.domain_id] = domain_id
        if module_name is not None:
            dat[KeyType.network_module] = module_name
        return self.send_msg(dat)

    def get_domain_peerlist(self, domain_id):
        """
        Get peer list of the domain from the core node (maybe used by a system administrator)

        :param domain_id:
        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_GET_PEERLIST)
        dat[KeyType.domain_id] = domain_id
        return self.send_msg(dat)

    def set_domain_static_node(self, domain_id, node_id, ipv4, ipv6, port):
        """
        Set static node to the core node (maybe used by a system administrator)

        :param domain_id:
        :param node_id:
        :param ipv4:
        :param ipv6:
        :param port:
        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_SET_STATIC_NODE)
        dat[KeyType.domain_id] = domain_id
        dat[KeyType.peer_info] = [node_id, ipv4, ipv6, port]
        return self.send_msg(dat)

    def send_domain_ping(self, domain_id, ipv4, ipv6, port):
        """
        Send domain ping to notify the existence of the node (maybe used by a system administrator)

        :param domain_id:
        :param ipv4:
        :param ipv6:
        :param port:
        :return:
        """
        dat = self.make_message_structure(None, MsgType.DOMAIN_PING)
        dat[KeyType.domain_id] = domain_id
        dat[KeyType.ipv4_address] = ipv4
        dat[KeyType.ipv6_address] = ipv6
        dat[KeyType.port_number] = port
        return self.send_msg(dat)

    def register_asset_group(self, domain_id, asset_group_id,
                             storage_type=StorageType.FILESYSTEM, storage_path=None,
                             advertise_in_domain0=False):
        """
        Register an asset_group in the core node (maybe used by a system administrator)

        :param domain_id:
        :param asset_group_id:
        :param storage_type:
        :param storage_path:
        :param advertise_in_domain0:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_SETUP_ASSET_GROUP)
        dat[KeyType.domain_id] = domain_id
        dat[KeyType.storage_type] = storage_type
        dat[KeyType.advertise_in_domain0] = advertise_in_domain0
        if storage_path is not None:
            dat[KeyType.storage_path] = storage_path
        return self.send_msg(dat)

    def get_bbc_config(self):
        """
        Get config file of bbc_core (maybe used by a system administrator)

        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_GET_CONFIG)
        return self.send_msg(dat)

    def get_domain_list(self):
        """
        Get domain_id list in bbc_core (maybe used by a system administrator)

        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_GET_DOMAINLIST)
        return self.send_msg(dat)

    def manipulate_ledger_subsystem(self, enable=False, domain_id=None):
        """
        start/stop ledger_subsystem on the bbc_core (maybe used by a system administrator)

        :param enable: True->start, False->stop
        :param domain_id: 
        :return:
        """
        dat = self.make_message_structure(None, MsgType.REQUEST_MANIP_LEDGER_SUBSYS)
        dat[KeyType.ledger_subsys_manip] = enable
        dat[KeyType.domain_id] = domain_id
        return self.send_msg(dat)

    def register_to_core(self):
        """
        Register the client (user_id) to the core node. After that, the client can communicate with the core node

        :return:
        """
        for asset_group_id in self.asset_groups:
            dat = self.make_message_structure(asset_group_id, MsgType.REGISTER)
            self.send_msg(dat)
        return True

    def unregister_from_core(self):
        """
        Unregister and disconnect from the core node

        :return:
        """
        dat = self.make_message_structure(None, MsgType.UNREGISTER)
        return self.send_msg(dat)

    def get_cross_refs(self, asset_group_id, number):
        """
        Get cross_refs

        :param asset_group_id:
        :param number:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_CROSS_REF)
        dat[KeyType.count] = number
        return self.send_msg(dat)

    def gather_signatures(self, asset_group_id, tx_obj, reference_obj=None, destinations=None, asset_files=None):
        """
        Request to gather signatures from the specified user_ids

        :param asset_group_id:
        :param tx_obj:
        :param reference_obj: BBcReference object
        :param destinations: list of destination user_ids
        :param asset_files: dictionary of {asset_id: file_content}
        :return:
        """
        if reference_obj is None and destinations is None:
            return False
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_GATHER_SIGNATURE)
        dat[KeyType.transaction_data] = tx_obj.serialize()
        if reference_obj is not None:
            dat[KeyType.destination_user_ids] = reference_obj.get_destinations()
            referred_transactions = dict()
            referred_transactions.update(reference_obj.get_referred_transaction())
            if len(referred_transactions) > 0:
                dat[KeyType.transactions] = referred_transactions
        elif destinations is not None:
            dat[KeyType.destination_user_ids] = destinations
        if isinstance(asset_files, dict):
            dat[KeyType.all_asset_files] = asset_files
        return self.send_msg(dat)

    def sendback_signature(self, asset_group_id, dst, ref_index, sig):
        """
        Send back the signed transaction to the source

        :param asset_group_id:
        :param dst:
        :param ref_index: Which reference in transaction the signature is for
        :param sig:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.RESPONSE_SIGNATURE)
        dat[KeyType.destination_user_id] = dst
        dat[KeyType.ref_index] = ref_index
        dat[KeyType.signature] = sig.serialize()
        return self.send_msg(dat)

    def sendback_denial_of_sign(self, asset_group_id, dst, reason_text):
        """
        Send back the denial of sign the transaction

        :param asset_group_id:
        :param dst:
        :param reason_text:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.RESPONSE_SIGNATURE)
        dat[KeyType.destination_user_id] = dst
        dat[KeyType.status] = EOTHER
        dat[KeyType.reason] = reason_text
        return self.send_msg(dat)

    def insert_transaction(self, asset_group_id, tx_obj):
        """
        Request to insert a legitimate transaction

        :param asset_group_id:
        :param tx_obj: Transaction object (not deserialized one)
        :return:
        """
        if tx_obj.transaction_id is None:
            tx_obj.digest()
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_INSERT)
        dat[KeyType.transaction_data] = tx_obj.serialize()
        ast = dict()
        for evt in tx_obj.events:
            if evt.asset is None:
                continue
            asset_digest, content = evt.asset.get_asset_file()
            if content is not None:
                ast[evt.asset.asset_id] = content
        dat[KeyType.all_asset_files] = ast
        return self.send_msg(dat)

    def search_asset(self, asset_group_id, asset_id):
        """
        Search request for the specified asset. This would return transaction_data (and asset_file file content)

        :param asset_group_id:
        :param asset_id:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_SEARCH_ASSET)
        dat[KeyType.asset_id] = asset_id
        return self.send_msg(dat)

    def search_transaction(self, asset_group_id, transaction_id):
        """
        Search request for transaction_data

        :param asset_group_id:
        :param transaction_id:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_SEARCH_TRANSACTION)
        dat[KeyType.transaction_id] = transaction_id
        return self.send_msg(dat)

    def register_in_ledger_subsystem(self, asset_group_id, transaction_id):
        """
        Register transaction_id in the ledger_subsystem

        :param asset_group_id:
        :param transaction_id:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_REGISTER_HASH_IN_SUBSYS)
        dat[KeyType.transaction_id] = transaction_id
        return self.send_msg(dat)

    def verify_in_ledger_subsystem(self, asset_group_id, transaction_id):
        """
        Verify transaction_id in the ledger_subsystem

        :param asset_group_id:
        :param transaction_id:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.REQUEST_VERIFY_HASH_IN_SUBSYS)
        dat[KeyType.transaction_id] = transaction_id
        return self.send_msg(dat)

    def send_message(self, msg, asset_group_id, dst_user_id):
        """
        Send peer-to-peer message to the specified user_id

        :param msg:
        :param asset_group_id:
        :param dst_user_id:
        :return:
        """
        dat = self.make_message_structure(asset_group_id, MsgType.MESSAGE)
        dat[KeyType.destination_user_id] = dst_user_id
        dat[KeyType.message] = msg
        return self.send_msg(dat)

    def start_receiver_loop(self):
        jobs = [gevent.spawn(self.receiver_loop)]
        #gevent.joinall(jobs)

    def receiver_loop(self):
        msg_parser = message_key_types.Message()
        try:
            while True:
                buf = self.connection.recv(8192)
                if len(buf) == 0:
                    break
                msg_parser.recv(buf)
                while True:
                    msg = msg_parser.parse()
                    if msg is None:
                        break
                    self.callback.dispatch(msg, msg_parser.payload_type)
        except Exception as e:
            self.logger.info("TCP disconnect: %s" % e)
            print(traceback.format_exc())
        self.connection.close()


class Callback:
    """
    Set of callback functions for processing received message
    """
    def __init__(self, log=None):
        self.logger = log
        self.queue = queue.Queue()

    def set_logger(self, log):
        self.logger = log

    def dispatch(self, dat, payload_type):
        #self.logger.debug("Received: %s" % dat)
        if KeyType.command not in dat:
            self.logger.warn("No command exists")
            return
        if dat[KeyType.command] == MsgType.RESPONSE_SEARCH_TRANSACTION:
            self.proc_resp_search_transaction(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_SEARCH_ASSET:
            self.proc_resp_search_asset(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_GATHER_SIGNATURE:
            self.proc_resp_gather_signature(dat)
        elif dat[KeyType.command] == MsgType.REQUEST_SIGNATURE:
            self.proc_cmd_sign_request(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_SIGNATURE:
            self.proc_resp_sign_request(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_INSERT:
            self.proc_resp_insert(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_CROSS_REF:
            self.proc_resp_cross_ref(dat)
        elif dat[KeyType.command] == MsgType.MESSAGE:
            self.proc_user_message(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_REGISTER_HASH_IN_SUBSYS:
            self.proc_resp_register_hash(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_VERIFY_HASH_IN_SUBSYS:
            self.proc_resp_verify_hash(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_SETUP_ASSET_GROUP:
            self.proc_resp_asset_group_setup(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_SETUP_DOMAIN:
            self.proc_resp_domain_setup(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_GET_PEERLIST:
            self.proc_resp_get_peerlist(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_GET_DOMAINLIST:
            self.proc_resp_get_domainlist(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_SET_STATIC_NODE:
            self.proc_resp_set_peer(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_GET_CONFIG:
            self.proc_resp_get_config(dat)
        elif dat[KeyType.command] == MsgType.RESPONSE_MANIP_LEDGER_SUBSYS:
            self.proc_resp_ledger_subsystem(dat)

        else:
            self.logger.warn("No method to process for command=%d" % dat[KeyType.command])

    def synchronize(self, timeout=None):
        """
        Wait for receiving message

        :param timeout: timeout second for waiting
        :return:
        """
        try:
            return self.queue.get(timeout=timeout)
        except:
            return None

    def proc_resp_cross_ref(self, dat):
        cross_refs = []
        for cross_ref in dat[KeyType.cross_refs]:
            cross = bbclib.BBcCrossRef(cross_ref[0], cross_ref[1])
            cross_refs.append(cross)
        self.queue.put(cross_refs)

    def proc_cmd_sign_request(self, dat):
        self.queue.put(dat)

    def proc_resp_sign_request(self, dat):
        self.queue.put(dat)

    def proc_resp_gather_signature(self, dat):
        if KeyType.status not in dat or dat[KeyType.status] < ESUCCESS:
            self.queue.put(dat)
            return
        sig = bbclib.recover_signature_object(dat[KeyType.signature])
        self.queue.put({KeyType.status: ESUCCESS, KeyType.result: (dat[KeyType.ref_index], dat[KeyType.source_user_id], sig)})

    def proc_resp_insert(self, dat):
        self.queue.put(dat)

    def proc_resp_search_asset(self, dat):
        self.queue.put(dat)

    def proc_resp_search_transaction(self, dat):
        if KeyType.transaction_data in dat:
            tx_obj = bbclib.recover_transaction_object_from_rawdata(dat[KeyType.transaction_data])
            tx_obj.digest()
            digest = tx_obj.digest()
            for i in range(len(tx_obj.signatures)):
                result = tx_obj.signatures[i].verify(digest)
                if not result:
                    dat = {KeyType.status: EBADTXSIGNATURE, KeyType.reason: "Verify failure", KeyType.transaction_data: tx_obj}
                    break
        self.queue.put(dat)

    def proc_user_message(self, dat):
        self.queue.put(dat)

    def proc_resp_register_hash(self, dat):
        self.queue.put(dat)

    def proc_resp_verify_hash(self, dat):
        self.queue.put(dat)

    def proc_resp_asset_group_setup(self, dat):
        self.queue.put(dat)

    def proc_resp_domain_setup(self, dat):
        self.queue.put(dat)

    def proc_resp_get_peerlist(self, dat):
        """
        Return node info

        :param dat:
        :return: list of node info (the first one is that of the connecting core)
        """
        if KeyType.peer_list not in dat:
            self.queue.put(None)
            return
        peerlist = dat[KeyType.peer_list]
        results = []
        count = int.from_bytes(peerlist[:4], 'little')
        for i in range(count):
            base = 4 + i*(32+4+16+2)
            node_id = peerlist[base:base+32]
            ipv4 = peerlist[base+32:base+36]
            ipv6 = peerlist[base+36:base+52]
            port = peerlist[base+52:base+54]
            info = NodeInfo()
            info.recover_nodeinfo(node_id, ipv4, ipv6, port)
            results.append([info.node_id, info.ipv4, info.ipv6, info.port])
        self.queue.put(results)

    def proc_resp_get_domainlist(self, dat):
        """
        Return domain_ids

        :param dat:
        :return: list of domain_id
        """
        if KeyType.domain_list not in dat:
            self.queue.put(None)
            return
        domainlist = dat[KeyType.domain_list]
        results = []
        count = int.from_bytes(domainlist[:2], 'big')
        for i in range(count):
            base = 2 + 32*i
            domain_id = domainlist[base:base+32]
            results.append(domain_id)
        self.queue.put(results)

    def proc_resp_set_peer(self, dat):
        self.queue.put(dat)

    def proc_resp_get_config(self, dat):
        self.queue.put(dat)

    def proc_resp_ledger_subsystem(self, dat):
        self.queue.put(dat)


