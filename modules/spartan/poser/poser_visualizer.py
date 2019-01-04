# system
import os
import yaml
from yaml import CLoader
import numpy as np

import director.objectmodel as om
import director.visualization as vis
from director import ioUtils
from director import transformUtils



class PoserVisualizer(object):

    def __init__(self, poser_output_folder = None):
        self._clear_visualization()
        self._poser_output_folder = poser_output_folder

    @property
    def poser_output_folder(self):
        """
        The full path to the poser output folder
        :return:
        :rtype:
        """
        return self._poser_output_folder

    @poser_output_folder.setter
    def poser_output_folder(self, value):
        self._poser_output_folder = value

    def load_poser_response(self):
        """
        Load the poser_response.yaml file
        :return:
        :rtype: dict
        """

        filename = self._convert_relative_path_to_absolute("poser_response.yaml")
        return yaml.load(file(filename), Loader=CLoader)


    def _convert_relative_path_to_absolute(self, path):
        """
        Converts a path that is relative to self.poser_output_folder to an
        absolute path.

        You must ensure that self.poser_output_folder is not
        None before calling this function
        :param path:
        :type path:
        :return:
        :rtype:
        """
        if self._poser_output_folder is None:
            raise ValueError("poser_output_folder cannot be None")

        return os.path.join(self._poser_output_folder, path)

    def _clear_visualization(self):
        """
        Delete the Poser vis container, create a new one with the same name
        :return:
        :rtype:
        """
        self._poser_vis_container = om.getOrCreateContainer("Poser")
        om.removeFromObjectModel(self._poser_vis_container)
        self._poser_vis_container = om.getOrCreateContainer("Poser")


    def visualize_result(self, poser_response=None):
        """
        Visualizes the results of running poser

        :param poser_response:
        :type poser_response: dict loaded from poser_response.yaml file
        :return:
        :rtype:
        """

        if poser_response is None:
            poser_response = self.load_poser_response()

        self._clear_visualization()
        self._object_vis_containers = dict()

        # visualize the observation
        for object_name, data in poser_response.iteritems():
            vis_dict = dict()
            self._object_vis_containers[object_name] = vis_dict
            vis_dict['container'] = om.getOrCreateContainer(object_name,
                                                            parentObj=self._poser_vis_container)

            # transform from template to observation
            T_obs_template = PoserVisualizer.parse_transform(data['rigid_transform'])

            # usually a pcd
            template_file = self._convert_relative_path_to_absolute(data['image_1']['save_template'])
            template_poly_data = ioUtils.readPolyData(template_file)
            vis_dict['template'] = vis.updatePolyData(template_poly_data, 'template', parent=vis_dict['container'],
                               color=[0,1,0])

            T = T_obs_template.GetLinearInverse()
            vis_dict['template'].actor.SetUserTransform(T_obs_template)



            # usually a pcd
            observation_file = self._convert_relative_path_to_absolute(data['image_1']['save_processed_cloud'])

            observation_poly_data = ioUtils.readPolyData(observation_file)
            vis_dict['observation'] = vis.updatePolyData(observation_poly_data, 'observation',
                                                       parent=vis_dict['container'], color=[1,0,0])



    def get_model_to_object_transform(self, object_name):
        """
        Returns the transform from model to object for the given object

        :param object_name: str
        :return: vtkTransform
        """

        T_obs_model = self._object_vis_containers[object_name]['template'].actor.GetUserTransform()
        return transformUtils.copyFrame(T_obs_model)

    def get_template_poly_data(self, object_name):
        """
        Returns the poly data for the template of the given object
        :param object_name: str
        :return: vtkPolyData
        """
        return self._object_vis_containers[object_name]['template'].polyData

    def get_observation_poly_data(self, object_name):
        """
        Returns the poly data for the given object
        :param object_name: str
        :return: vtkPolyData
        """
        return self._object_vis_containers[object_name]['observation'].polyData

    @staticmethod
    def parse_transform(transform_matrix_list):
        """
        Returns a vtkTransform matrix from column major list of matrix coefficients
        :param transform_matrix_list:
        :type transform_matrix_list:
        :return:
        :rtype:
        """

        matrix_coeffs = np.array(transform_matrix_list) # vector of length 16
        mat = np.reshape(matrix_coeffs, [4,4], order='F')
        return transformUtils.getTransformFromNumpy(mat)

    @staticmethod
    def make_default():
        """
        Makes poser
        :return:
        """
        # spartan
        import spartan.utils.utils as spartanUtils

        path_to_poser_output = os.path.join(spartanUtils.get_sandbox_dir(), "poser")

        if not os.path.exists(path_to_poser_output):
            raise ValueError("poser output folder %s doesn't exist" %(path_to_poser_output))

        return PoserVisualizer(path_to_poser_output)