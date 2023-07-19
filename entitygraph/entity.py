import io
import json
import os
from pathlib import Path
from random import randint
from typing import List, BinaryIO, TextIO
from urllib.parse import urlparse

from rdflib import Graph, Literal, URIRef, XSD, SDO, RDF, Namespace

import entitygraph
from entitygraph import EntitiesAPI
from entitygraph.api_response import ApiResponse
from entitygraph.namespace_map import namespace_map


class EntityIterable:
    def __init__(self, api: EntitiesAPI, application_label: str, property: str = None):
        self.api: EntitiesAPI = api
        self.application_label: str = application_label
        self.property: str = property
        self.cache: List[Entity] = []

    def __getitem__(self, item):
        tmp = None

        if isinstance(item, slice):
            start = item.start or 0
            stop = item.stop or 10
            step = item.step or 1

            if step != 1:
                raise ValueError('Step values other than 1 are not supported')

            tmp = json.loads(self.api.list(offset=start, limit=stop - start, application_label=self.application_label,
                                           response_mimetype='application/ld+json').text)['@graph']
        elif isinstance(item, int):
            tmp = json.loads(self.api.list(offset=0, limit=item, application_label=self.application_label,
                                           response_mimetype='application/ld+json').text)['@graph']
        else:
            tmp = json.loads(self.api.list(offset=0, limit=10, application_label=self.application_label,
                                           response_mimetype='application/ld+json').text)['@graph']

        for x in tmp:
            parsed_url = urlparse(x['@id'])
            path_parts = parsed_url.path.strip('/').split('/')
            entity_id = path_parts[-1] if len(path_parts) > 0 else None

            res = self.api.read(entity_id, self.property, self.application_label, 'text/turtle')
            tmp = Entity(data=res.text, format='turtle')
            tmp._Entity__application_label = self.application_label
            self.cache.append(tmp)

        return self.cache


class Entity:
    def __init__(self, data: Graph | str | dict = None, format: str = 'turtle'):
        if entitygraph.client is not None:
            self.__api: EntitiesAPI = entitygraph.client.entities_api
        else:
            raise Exception(
                "Not connected. Please connect using entitygraph.connect(api_key=..., host=...) before using Entity()")

        self._application_label: str = "default"
        self._id: str = None

        if isinstance(data, Graph):
            self.__graph: Graph = data
        else:
            if format == 'turtle':
                self.__graph: Graph = Graph().parse(data=data, format='turtle',
                                                    encoding='utf-8') if data is not None else None
            elif format == 'json-ld':
                self.__graph: Graph = Graph().parse(data=data, format='json-ld',
                                                    encoding='utf-8') if data is not None else None
            elif format == 'n3':
                self.__graph: Graph = Graph().parse(data=data, format='n3',
                                                    encoding='utf-8') if data is not None else None
            else:
                raise ValueError(f"Unsupported format: {format}")

    def __check_id(self):
        if not self._id:
            raise Exception(
                "This entity has not been saved yet or does not exist. Please call .save() first to save the entity or use .get_by_id() to retrieve an existing entity.")

    def __str__(self):
        return self.turtle()

    def as_graph(self) -> Graph:
        return self.__graph

    def turtle(self) -> str:
        return self.__graph.serialize(format='turtle', encoding='utf-8').decode('utf-8')

    def json(self) -> dict:
        return json.loads(self.__graph.serialize(format='json-ld', encoding='utf-8').decode('utf-8'))

    def n3(self) -> str:
        return self.__graph.serialize(format='n3', encoding='utf-8').decode('utf-8')

    @property
    def uri(self) -> URIRef:
        return URIRef(f"{entitygraph.client.base_url}/api/s/{self._application_label}/entities/{self._id}")

    def __uriref_to_prefixed(self, url: URIRef) -> str:
        if not isinstance(url, URIRef):
            raise ValueError(
                f'Invalid input "{url}". Expected a URIRef instance, e.g., URIRef("https://schema.org/name") or "SDO.name"')

        url_obj = urlparse(str(url))
        stripped_url = url_obj.netloc + url_obj.path

        for key in namespace_map:
            if key in stripped_url:
                return stripped_url.replace(key, f"{namespace_map[key]}.")
        return stripped_url

    def save(self) -> 'Entity':
        response: ApiResponse = self.__api.create(self.turtle(), self._application_label, "text/turtle", "text/turtle")
        tmp = Graph().parse(data=response.text, format='turtle')
        for s, p, o in tmp:
            if 'entities' in str(s):
                parts = str(s).split('/')
                self._id = parts[-1]
                break

        response2: ApiResponse = self.__api.read(self._id, application_label=self._application_label,
                                                 response_mimetype='text/turtle')
        self.__graph = Graph().parse(data=response2.text, format='turtle')

        return self

    def update(self) -> 'Entity':
        """
        Retrieves the entity from the API and updates the local Entity object
        """
        self.__check_id()

        response: ApiResponse = self.__api.read(self._id, application_label=self._application_label,
                                                response_mimetype='text/turtle')
        self.__graph = Graph().parse(data=response.text, format='turtle')

        return self

    def get_by_id(self, entity_id: str) -> 'Entity':
        response: ApiResponse = self.__api.read(entity_id, application_label=self._application_label,
                                                response_mimetype='text/turtle')
        tmp = Entity(data=response.text)
        tmp._id = entity_id
        tmp._application_label = self._application_label
        return tmp

    def get_all(self, property: URIRef = None) -> List['Entity']:
        return EntityIterable(self.__api, self._application_label, self.__uriref_to_prefixed(property) if property else None)

    def delete_by_id(self, entity_id: str) -> None:
        response = self.__api.delete(entity_id, self._application_label)

    def set_value(self, property: URIRef, value: str | URIRef, language: str = 'en') -> Exception | None:
        """
        Sets a specific value.

        :param property: Property (qualified URL)
        :param value: Value (no longer than 255 chars)
        :param language: Language (defaults to "en")
        """
        self.__check_id()

        if len(value) > 255:
            raise ValueError('Value should not be longer than 255 chars')

        # Convert property to prefixed version
        prefixed = self.__uriref_to_prefixed(property)

        if isinstance(value, URIRef):
            value = '<' + str(value) + '>'

        return self.__api.set_value(entity_id=self._id,
                                    prefixed_key=prefixed,
                                    value=value,
                                    lang=language,
                                    application_label=self._application_label)

    def set_content(self, property: URIRef, content: Path | BinaryIO | TextIO | bytes | str,
                    filename: str = None) -> Exception | None:
        """
        Sets content.

        :param property: Property (qualified URL)
        :param content: Content (can be file, path, binary, string)
        :param filename: Filename (defaults to "file_{random}.bin")
        """
        self.__check_id()

        # Convert property to prefixed version
        prefixed = self.__uriref_to_prefixed(property)

        if isinstance(content, str):
            content_data = content.encode()
        elif isinstance(content, Path):
            with content.open('rb') as f:
                content_data = f.read()
        elif isinstance(content, (BinaryIO, TextIO)):
            content_data = content.read()
        else:
            content_data = content

        if not filename:
            filename = f'file_{randint(1, 999999999)}.bin'

        return self.__api.set_value(entity_id=self._id,
                                    prefixed_key=prefixed,
                                    value=content_data,
                                    filename=filename,
                                    application_label=self._application_label,
                                    request_mimetype='application/octet-stream',
                                    response_mimetype='text/turtle')

    def remove_value(self, property: URIRef, language: str = 'en') -> Exception | None:
        """
        Removes a property value.

        :param property: Property (qualified URL)
        :param language: Language (defaults to "en")
        """
        self.__check_id()

        # Convert property to prefixed version
        prefixed = self.__uriref_to_prefixed(property)

        return self.__api.remove_value(entity_id=self._id,
                                       prefixed_key=prefixed,
                                       lang=language,
                                       application_label=self._application_label)

    def create_edge(self, property: URIRef, target: 'Entity') -> Exception | None:
        """
        Create edge to existing entity (within the same dataset)

        :param property: Property (qualified URL)
        :param target: Target entity (must be saved first)
        """
        self.__check_id()

        if not target._id:
            raise Exception(
                "Target entity has not been saved yet or does not exist. Please call .save() first to save the entity or use .get_by_id() to retrieve an existing entity.")

        # Convert property to prefixed version
        prefixed = self.__uriref_to_prefixed(property)

        return self.__api.create_link(source_id=self._id,
                                      prefixed_key=prefixed,
                                      target_id=target._id,
                                      application_label=self._application_label)

    def delete_edge(self, property: URIRef, target: 'Entity') -> Exception | None:
        """
        Delete edge to existing entity (within the same dataset)

        :param property: Property (qualified URL)
        :param target: Target entity (must be saved first)
        """
        self.__check_id()

        if not target._id:
            raise Exception(
                "Target entity has not been saved yet or does not exist. Please call .save() first to save the entity or use .get_by_id() to retrieve an existing entity.")

        # Convert property to prefixed version
        prefixed = self.__uriref_to_prefixed(property)

        return self.__api.delete_link(source_id=self._id,
                                      prefixed_key=prefixed,
                                      target_id=target._id,
                                      application_label=self._application_label)
