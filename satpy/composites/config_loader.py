#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2020 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""Classes for loading compositor and modifier configuration files."""
import os
import logging
import warnings

import yaml
from yaml import UnsafeLoader

from satpy import DatasetDict, DataQuery, DataID
from satpy.config import CONFIG_PATH, get_entry_points_config_dirs, config_search_paths, recursive_dict_update
from satpy.dataset.dataid import minimal_default_keys_config

logger = logging.getLogger(__name__)


class CompositorLoader(object):
    """Read compositors and modifiers using the configuration files on disk."""

    def __init__(self, ppp_config_dir=None):
        """Initialize the compositor loader."""
        if ppp_config_dir is None:
            ppp_config_dir = CONFIG_PATH
        self.modifiers = {}
        self.compositors = {}
        self.ppp_config_dir = ppp_config_dir
        # sensor -> { dict of DataID key information }
        self._sensor_dataid_keys = {}

    def load_sensor_composites(self, sensor_name):
        """Load all compositor configs for the provided sensor."""
        config_filename = sensor_name + ".yaml"
        logger.debug("Looking for composites config file %s", config_filename)
        paths = get_entry_points_config_dirs('satpy.composites')
        paths.append(self.ppp_config_dir)
        composite_configs = config_search_paths(
            os.path.join("composites", config_filename),
            *paths, check_exists=True)
        if not composite_configs:
            logger.debug("No composite config found called {}".format(
                config_filename))
            return
        self._load_config(composite_configs)

    def get_compositor(self, key, sensor_names):
        """Get the modifier for *sensor_names*."""
        for sensor_name in sensor_names:
            try:
                return self.compositors[sensor_name][key]
            except KeyError:
                continue
        raise KeyError("Could not find compositor '{}'".format(key))

    def get_modifier(self, key, sensor_names):
        """Get the modifier for *sensor_names*."""
        for sensor_name in sensor_names:
            try:
                return self.modifiers[sensor_name][key]
            except KeyError:
                continue
        raise KeyError("Could not find modifier '{}'".format(key))

    def load_compositors(self, sensor_names):
        """Load all compositor configs for the provided sensors.

        Args:
            sensor_names (list of strings): Sensor names that have matching
                                            ``sensor_name.yaml`` config files.

        Returns:
            (comps, mods): Where `comps` is a dictionary:

                    sensor_name -> composite ID -> compositor object

                And `mods` is a dictionary:

                    sensor_name -> modifier name -> (modifier class,
                    modifiers options)

                Note that these dictionaries are copies of those cached in
                this object.

        """
        comps = {}
        mods = {}
        for sensor_name in sensor_names:
            if sensor_name not in self.compositors:
                self.load_sensor_composites(sensor_name)
            if sensor_name in self.compositors:
                comps[sensor_name] = DatasetDict(
                    self.compositors[sensor_name].copy())
                mods[sensor_name] = self.modifiers[sensor_name].copy()
        return comps, mods

    @staticmethod
    def _convert_dep_info_to_data_query(dep_info):
        key_item = dep_info.copy()
        key_item.pop('prerequisites', None)
        key_item.pop('optional_prerequisites', None)
        if 'modifiers' in key_item:
            key_item['modifiers'] = tuple(key_item['modifiers'])
        key = DataQuery.from_dict(key_item)
        return key

    @staticmethod
    def _get_modifier_loader_from_config(modifier_name, modifier_info):
        try:
            loader = modifier_info.pop('modifier', None)
            if loader is None:
                loader = modifier_info.pop('compositor')
                warnings.warn("Modifier '{}' uses deprecated 'compositor' "
                              "key to point to Python class, replace "
                              "with 'modifier'.".format(modifier_name))
        except KeyError:
            raise ValueError("'modifier' key missing or empty for '{}'. Option keys = {}".format(
                modifier_name, str(modifier_info.keys())))
        return loader

    def _process_modifier_deps(self, modifier_info):
        for prereq_type in ['prerequisites', 'optional_prerequisites']:
            prereqs = []
            for dep_info in modifier_info.get(prereq_type, []):
                if not isinstance(dep_info, dict):
                    prereqs.append(dep_info)
                    continue
                prereq_key = self._convert_dep_info_to_data_query(dep_info)
                prereqs.append(prereq_key)
            modifier_info[prereq_type] = prereqs

    def _load_config_modifier(self, modifier_info, loaded_modifiers):
        modifier_name = modifier_info['name']
        loader = self._get_modifier_loader_from_config(modifier_name, modifier_info)
        self._process_modifier_deps(modifier_info)
        loaded_modifiers[modifier_name] = (loader, modifier_info)

    def _load_config_modifiers(self, conf, loaded_modifiers):
        configured_modifiers = conf.get('modifiers', {})
        for modifier_name, modifier_info in configured_modifiers.items():
            modifier_info['name'] = modifier_name
            self._load_config_modifier(modifier_info, loaded_modifiers)

    @staticmethod
    def _create_comp_from_info(composite_info, sensor_id_keys, loader):
        key = DataID(sensor_id_keys, **composite_info)
        comp = loader(_satpy_id=key, **composite_info)
        return key, comp

    def _handle_inline_comp_dep(self, dep_info, dep_num, parent_name, loaded_compositors):
        # Create an unique temporary name for the composite
        sub_comp_name = '_' + parent_name + '_dep_{}'.format(dep_num)
        dep_info['name'] = sub_comp_name
        self._load_config_composite(dep_info, loaded_compositors,
                                    minimal_default_keys_config)

    @staticmethod
    def _get_compositor_loader_from_config(composite_name, composite_info):
        try:
            loader = composite_info.pop('compositor')
        except KeyError:
            raise ValueError("'compositor' key missing or empty for '{}'. Option keys = {}".format(
                composite_name, str(composite_info.keys())))
        return loader

    def _process_composite_deps(self, composite_info, sensor_composites):
        dep_num = -1
        for prereq_type in ['prerequisites', 'optional_prerequisites']:
            prereqs = []
            for dep_info in composite_info.get(prereq_type, []):
                dep_num += 1
                if not isinstance(dep_info, dict):
                    prereqs.append(dep_info)
                    continue
                elif 'compositor' in dep_info:
                    self._handle_inline_comp_dep(
                        dep_info, dep_num, composite_info['name'],
                        sensor_composites)
                prereq_key = self._convert_dep_info_to_data_query(dep_info)
                prereqs.append(prereq_key)
            composite_info[prereq_type] = prereqs

    def _load_config_composite(self, composite_info, loaded_compositors, sensor_id_keys):
        composite_name = composite_info['name']
        loader = self._get_compositor_loader_from_config(composite_name, composite_info)
        self._process_composite_deps(composite_info,
                                     loaded_compositors)
        key, comp = self._create_comp_from_info(composite_info, sensor_id_keys, loader)
        loaded_compositors[key] = comp

    def _load_config_composites(self, conf, loaded_compositors, sensor_id_keys):
        configured_modifiers = conf.get('composites', {})
        for composite_name, composite_info in configured_modifiers.items():
            composite_info['name'] = composite_name
            self._load_config_composite(composite_info, loaded_compositors,
                                        sensor_id_keys)

    def _get_sensor_id_keys(self, conf, sensor_id, sensor_deps):
        try:
            id_keys = conf['composite_identification_keys']
        except KeyError:
            try:
                id_keys = self._sensor_dataid_keys[sensor_deps[-1]]
            except IndexError:
                id_keys = minimal_default_keys_config
        self._sensor_dataid_keys[sensor_id] = id_keys
        return id_keys

    def _load_config(self, composite_configs):
        if not isinstance(composite_configs, (list, tuple)):
            composite_configs = [composite_configs]

        conf = {}
        for composite_config in composite_configs:
            with open(composite_config, 'r', encoding='utf-8') as conf_file:
                conf = recursive_dict_update(conf, yaml.load(conf_file, Loader=UnsafeLoader))
        try:
            sensor_name = conf['sensor_name']
        except KeyError:
            logger.debug('No "sensor_name" tag found in %s, skipping.',
                         composite_configs)
            return

        sensor_id = sensor_name.split('/')[-1]
        sensor_deps = sensor_name.split('/')[:-1]

        compositors = self.compositors.setdefault(sensor_id, DatasetDict())
        modifiers = self.modifiers.setdefault(sensor_id, {})

        for sensor_dep in reversed(sensor_deps):
            if sensor_dep not in self.compositors or sensor_dep not in self.modifiers:
                self.load_sensor_composites(sensor_dep)

        if sensor_deps:
            compositors.update(self.compositors[sensor_deps[-1]])
            modifiers.update(self.modifiers[sensor_deps[-1]])

        id_keys = self._get_sensor_id_keys(conf, sensor_id, sensor_deps)
        try:
            self._load_config_modifiers(conf, modifiers)
        except (ValueError, KeyError):
            raise RuntimeError("Failed to load modifiers from config "
                               "'{}'".format(composite_configs))

        try:
            self._load_config_composites(conf, compositors, id_keys)
        except (ValueError, KeyError):
            raise RuntimeError("Failed to load composites from config "
                               "'{}'".format(composite_configs))