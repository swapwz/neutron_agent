# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo.config import cfg
from oslo_log import log
import sqlalchemy as sa
from sqlalchemy import sql

from neutron.common import constants as n_const
from neutron.common import exceptions as exc
from neutron.db import api as db_api
from neutron.db import model_base
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2.drivers import type_tunnel

LOG = log.getLogger(__name__)

TYPE_H3C_VXLAN = 'h3c_vxlan'

h3c_vxlan_opts = [
    cfg.ListOpt('vni_ranges',
                default=[],
                help=_("Comma-separated list of <vni_min>:<vni_max> tuples "
                       "enumerating ranges of VXLAN VNI IDs that are "
                       "available for tenant network allocation")),
]

cfg.CONF.register_opts(h3c_vxlan_opts, "ml2_type_h3c_vxlan")


class H3CVxlanAllocation(model_base.BASEV2):

    __tablename__ = 'ml2_h3c_vxlan_allocations'

    vxlan_vni = sa.Column(sa.Integer, nullable=False, primary_key=True,
                          autoincrement=False)
    allocated = sa.Column(sa.Boolean, nullable=False, default=False,
                          server_default=sql.false(), index=True)


class H3CVxlanTypeDriver(type_tunnel.TunnelTypeDriver):
    def __init__(self):
        LOG.info("H3CVxlanTypeDriver init")
        super(H3CVxlanTypeDriver, self).__init__(H3CVxlanAllocation)

    def get_type(self):
        return TYPE_H3C_VXLAN

    def initialize(self):
        LOG.info("H3CVxlanTypeDriver initialize")
        self.tunnel_ranges = []
        self._verify_vni_ranges()
        self.sync_allocations()

    def _verify_vni_ranges(self):
        try:
            self._parse_h3c_vni_ranges(cfg.CONF.ml2_type_h3c_vxlan.vni_ranges,
                                       self.tunnel_ranges)
        except Exception:
            LOG.exception("Failed to parse vni_ranges. "
                          "Service terminated!")
            raise SystemExit()

    def _parse_h3c_vni_ranges(self, tunnel_ranges, current_range):
        for entry in tunnel_ranges:
            entry = entry.strip()
            try:
                tun_min, tun_max = entry.split(':')
                tun_min = tun_min.strip()
                tun_max = tun_max.strip()
                tunnel_range = int(tun_min), int(tun_max)
            except ValueError as ex:
                raise exc.NetworkTunnelRangeError(tunnel_range=entry, error=ex)

            self._parse_h3c_vni_range(tunnel_range)
            current_range.append(tunnel_range)

        LOG.info(("H3C VXLAN ID ranges: %(range)s"),
                 {'range': current_range})

    def _parse_h3c_vni_range(self, tunnel_range):
        """Raise an exception for invalid tunnel range or malformed range."""
        for ident in tunnel_range:
            if not self._is_valid_h3c_vni(ident):
                raise exc.NetworkTunnelRangeError(
                    tunnel_range=tunnel_range,
                    error=_("%(id)s is not a valid H3C VNI value.") %
                    {'id': ident})

        if tunnel_range[1] < tunnel_range[0]:
            raise exc.NetworkTunnelRangeError(
                tunnel_range=tunnel_range,
                error=_("End of tunnel range is less than start of "
                        "tunnel range."))

    def _is_valid_h3c_vni(self, vni):
        return n_const.MIN_VXLAN_VNI <= vni <= n_const.MAX_VXLAN_VNI

    def allocate_tenant_segment(self, session):
        alloc = self.allocate_partially_specified_segment(session)
        if not alloc:
            return
        return {api.NETWORK_TYPE: TYPE_H3C_VXLAN,
                api.PHYSICAL_NETWORK: None,
                api.SEGMENTATION_ID: alloc.vxlan_vni}

    def sync_allocations(self):
        vxlan_vnis = set()
        for tun_min, tun_max in self.tunnel_ranges:
            vxlan_vnis |= set(xrange(tun_min, tun_max + 1))

        session = db_api.get_session()
        with session.begin(subtransactions=True):
            # remove from table unallocated tunnels not currently allocatable
            # fetch results as list via all() because we'll be iterating
            # through them twice
            allocs = (session.query(H3CVxlanAllocation).
                      with_lockmode("update").all())
            # collect all vnis present in db
            existing_vnis = set(alloc.vxlan_vni for alloc in allocs)
            # collect those vnis that needs to be deleted from db
            vnis_to_remove = [alloc.vxlan_vni for alloc in allocs
                              if (alloc.vxlan_vni not in vxlan_vnis and
                                  not alloc.allocated)]
            # Immediately delete vnis in chunks. This leaves no work for
            # flush at the end of transaction
            bulk_size = 100
            chunked_vnis = (vnis_to_remove[i:i + bulk_size] for i in
                            range(0, len(vnis_to_remove), bulk_size))
            for vni_list in chunked_vnis:
                if vni_list:
                    session.query(H3CVxlanAllocation).filter(
                        H3CVxlanAllocation.vxlan_vni.in_(vni_list)).delete(
                            synchronize_session=False)
            # collect vnis that need to be added
            vnis = list(vxlan_vnis - existing_vnis)
            chunked_vnis = (vnis[i:i + bulk_size] for i in
                            range(0, len(vnis), bulk_size))
            for vni_list in chunked_vnis:
                bulk = [{'vxlan_vni': vni, 'allocated': False}
                        for vni in vni_list]
                session.execute(H3CVxlanAllocation.__table__.insert(), bulk)

    def reserve_provider_segment(self, session, segment):
        if self.is_partial_segment(segment):
            alloc = self.allocate_partially_specified_segment(session)
            if not alloc:
                raise exc.NoNetworkAvailable
        else:
            segmentation_id = segment.get(api.SEGMENTATION_ID)
            alloc = self.allocate_fully_specified_segment(
                session, vxlan_vni=segmentation_id)
            if not alloc:
                raise exc.TunnelIdInUse(tunnel_id=segmentation_id)
        return {api.NETWORK_TYPE: p_const.TYPE_VXLAN,
                api.PHYSICAL_NETWORK: None,
                api.SEGMENTATION_ID: alloc.vxlan_vni}

    def release_segment(self, session, segment):
        vxlan_vni = segment[api.SEGMENTATION_ID]

        inside = any(lo <= vxlan_vni <= hi for lo, hi in self.tunnel_ranges)

        with session.begin(subtransactions=True):
            query = (session.query(H3CVxlanAllocation).
                     filter_by(vxlan_vni=vxlan_vni))
            if inside:
                count = query.update({"allocated": False})
                if count:
                    LOG.debug("Releasing vxlan tunnel %s to pool",
                              vxlan_vni)
            else:
                count = query.delete()
                if count:
                    LOG.debug("Releasing vxlan tunnel %s outside pool",
                              vxlan_vni)
        if not count:
            LOG.warning(_("vxlan_vni %s not found"), vxlan_vni)

    def add_endpoint(self, ip, host):
        pass

    def get_endpoints(self):
        pass

    def get_endpoint_by_host(self, host):
        pass

    def get_endpoint_by_ip(self, ip):
        pass

    def delete_endpoint(self, ip):
        pass
