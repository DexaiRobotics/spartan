import os

# director
import director.ioUtils as ioUtils
import director.objectmodel as om
import director.visualization as vis
import director.transformUtils as transformUtils
# spartan
import spartan.utils.utils as spartan_utils

class CategoryManipulation(object):

    def __init__(self, robotStateModel):
        self._robotStateModel = robotStateModel
        self.clear_visualation()
        self.setup_config()
        self._gripper_link_frame_name = "wsg_50_base_link"

    def setup_config(self):
        """

        :return:
        :rtype:
        """
        self._config = dict()

        self._config['horizontal_mug_grasp'] = dict()
        self._config['horizontal_mug_grasp']['pos'] = [ 0.0006184 , -0.00910936,  0.12328004]
        self._config['horizontal_mug_grasp']['quat'] = [ 0.46905282,  0.48515971, -0.5104567 ,  0.53295729]


    def setup_horizontal_mug_grasp(self):
        self.load_object_model()

        name = "horizontal_mug_grasp"
        pos = self._config[name]['pos']
        quat = self._config[name]['quat']
        self._T_gripper_object = transformUtils.transformFromPose(pos, quat)
        self.update()


    def load_object_model(self):
        """
        Load the object model poly data
        :return:
        :rtype:
        """
        model_filename = os.path.join(spartan_utils.get_data_dir(), "pdc/templates/mugs/mug.stl")
        print("model_filename", model_filename)
        self._poly_data = ioUtils.readPolyData(model_filename)
        self._object = vis.updatePolyData(self._poly_data, "Object", parent=self._vis_container)
        vis.addChildFrame(self._object)



        pos = [0.34301733, 0.00805546, 0.92509635]
        quat = [0.17589242, 0.98340013, -0.04305877, 0.01148846]

        t = transformUtils.transformFromPose(pos, quat)
        self._object.getChildFrame().copyFrame(t)


    def clear_visualation(self):
        """
        Clear the vis container
        :return:
        :rtype:
        """
        container_name = "Category Manipulation"
        c = om.getOrCreateContainer(container_name)
        om.removeFromObjectModel(c)
        self._vis_container = om.getOrCreateContainer(container_name)


    def spawn_object(self):
        pass


    def update(self):
        """
        Updates the location of all objects. Use a `connectFrameModified` callback
        :return:
        :rtype:
        """
        T_world_gripper = self._robotStateModel.getLinkFrame(self._gripper_link_frame_name)
        T_world_object = transformUtils.concatenateTransforms([self._T_gripper_object, T_world_gripper])
        self._object.getChildFrame().copyFrame(T_world_object)


    def get_object_pose_relative_to_gripper(self):
        T_world_object = self._object.actor.GetUserTransform()
        T_world_gripper = self._robotStateModel.getLinkFrame(self._gripper_link_frame_name)

        T_gripper_object = transformUtils.concatenateTransforms([T_world_object, T_world_gripper.GetLinearInverse()])

        print(transformUtils.poseFromTransform(T_gripper_object))
        return T_gripper_object
